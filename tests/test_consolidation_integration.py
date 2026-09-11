"""Consolidation end to end: two reports of one disruption must be one event.

Uses :class:`SameVectorProvider` so documents cluster on purpose; the rules for what the
second document may change are exercised against a live database.

Run with:  pytest -m integration
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gri.ingestion.normalise import content_hash, normalise_text, sha256_text
from gri.models import Event, EventDocument, EventEvidence, Extraction, Prompt, RawDocument, Source
from gri.processing.embed import SameVectorProvider
from gri.processing.extract import ScriptedExtractionProvider
from gri.processing.pipeline import PipelineResult, process_document
from gri.review import apply_decision
from gri.schemas import ExtractedLocation, ExtractionResult
from tests import factories

pytestmark = pytest.mark.integration

FIRST = factories.NOTICE_TEXT
FOLLOW_UP = normalise_text(
    "Example Port remains closed to all traffic following the channel obstruction. "
    "The Example Port Authority now expects delays of up to ten days. "
    "Vessels are being held at Example Anchorage."
)


def run(
    session: Session,
    source: Source,
    text: str,
    result: ExtractionResult,
    published: datetime = datetime(2026, 9, 1, 8, 30, tzinfo=UTC),
) -> PipelineResult:
    marker = uuid.uuid4().hex
    document = RawDocument(
        source_id=source.id,
        url=f"https://notices.example.invalid/{marker}",
        title="Example Port notice",
        published_at=published,
        retrieved_at=published,
        retrieval_method="rss",
        clean_text=text,
        raw_body=text,
        raw_hash=sha256_text(text),
        content_hash=content_hash(text + marker),
    )
    session.add(document)
    session.flush()
    return process_document(
        session,
        document,
        embedding_provider=SameVectorProvider(),
        extraction_provider=ScriptedExtractionProvider([result]),
    )


def events(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Event)) or 0


def follow_up_result(
    severity: str = "moderate", quotes: list[tuple[str, str]] | None = None
) -> ExtractionResult:
    quotes = quotes or [("summary", "Example Port remains closed to all traffic")]
    return factories.make_result(severity=severity, quotes=quotes)


class TestDuplicates:
    def test_a_second_report_of_the_same_facts_is_one_event(
        self, session: Session, source: Source
    ) -> None:
        first = run(session, source, FIRST, factories.make_result())
        second = run(session, source, FOLLOW_UP, follow_up_result())

        assert events(session) == 1
        assert second.event_id == first.event_id
        assert (first.relation, second.relation) == ("new_event", "duplicate")

    def test_a_duplicate_adds_corroborating_evidence_from_its_own_source(
        self, session: Session, source: Source
    ) -> None:
        first = run(session, source, FIRST, factories.make_result())
        second = run(session, source, FOLLOW_UP, follow_up_result())

        documents = {
            row.document_id
            for row in session.scalars(
                select(EventEvidence).where(EventEvidence.event_id == first.event_id)
            )
        }
        assert len(documents) == 2
        relations = sorted(
            r.relation
            for r in session.scalars(
                select(EventDocument).where(EventDocument.event_id == first.event_id)
            )
        )
        assert relations == ["duplicate", "new_event"]
        assert second.published

    def test_a_duplicate_changes_nothing_on_the_record(
        self, session: Session, source: Source
    ) -> None:
        first = run(session, source, FIRST, factories.make_result())
        event = session.get(Event, first.event_id)
        assert event is not None
        before = (event.severity, event.title, event.change_note, event.requires_human_review)

        run(session, source, FOLLOW_UP, follow_up_result())

        assert (
            event.severity,
            event.title,
            event.change_note,
            event.requires_human_review,
        ) == before

    def test_documents_outside_the_time_window_are_separate_events(
        self, session: Session, source: Source
    ) -> None:
        """The same place, months apart, is a new disruption -- not an update."""
        run(session, source, FIRST, factories.make_result())
        run(
            session,
            source,
            FOLLOW_UP,
            follow_up_result(),
            published=datetime(2026, 12, 1, tzinfo=UTC),
        )
        assert events(session) == 2


class TestUpdates:
    def test_a_supported_escalation_updates_the_record_with_a_quoted_note(
        self, session: Session, source: Source
    ) -> None:
        first = run(session, source, FIRST, factories.make_result())
        second = run(
            session,
            source,
            FOLLOW_UP,
            follow_up_result(
                severity="high",
                quotes=[
                    ("summary", "Example Port remains closed to all traffic"),
                    ("severity", "now expects delays of up to ten days"),
                ],
            ),
        )

        event = session.get(Event, first.event_id)
        assert event is not None
        assert events(session) == 1
        assert second.relation == "update"
        assert event.severity == "high"
        assert event.change_note is not None
        assert '"now expects delays of up to ten days"' in event.change_note
        # High severity always needs a human (trigger 4, a database CHECK).
        assert event.requires_human_review is True
        assert event.review_status == "pending"

    def test_an_unsupported_escalation_is_not_applied_and_is_held(
        self, session: Session, source: Source
    ) -> None:
        first = run(session, source, FIRST, factories.make_result())
        run(session, source, FOLLOW_UP, follow_up_result(severity="high"))

        event = session.get(Event, first.event_id)
        assert event is not None
        assert event.severity == "moderate"
        assert event.requires_human_review is True
        assert (
            event.change_note is not None and "no verified quote supports it" in event.change_note
        )

    def test_a_grounded_new_location_is_added(self, session: Session, source: Source) -> None:
        first = run(session, source, FIRST, factories.make_result())
        added = follow_up_result(
            quotes=[("summary", "Vessels are being held at Example Anchorage")]
        )
        assert added.event is not None
        added = added.model_copy(
            update={
                "event": added.event.model_copy(
                    update={
                        "locations": [
                            *added.event.locations,
                            ExtractedLocation(
                                name="Example Anchorage", location_type="port", precision="facility"
                            ),
                        ]
                    }
                )
            }
        )

        second = run(session, source, FOLLOW_UP, added)

        event = session.get(Event, first.event_id)
        assert event is not None
        assert second.relation == "update"
        assert sorted(loc.name for loc in event.locations) == ["Example Anchorage", "Example Port"]
        # Adding a place is new content but triggers no review rule on its own.
        assert event.requires_human_review is False

    def test_a_conflicting_date_leaves_the_record_and_holds_it(
        self, session: Session, source: Source
    ) -> None:
        from datetime import date

        first = run(session, source, FIRST, factories.make_result())
        conflicting = factories.make_result(
            event_date=date(2026, 9, 5),
            quotes=[("summary", "Example Port remains closed to all traffic")],
        )
        run(session, source, FOLLOW_UP, conflicting)

        event = session.get(Event, first.event_id)
        assert event is not None
        assert event.event_date == date(2026, 9, 1)
        assert event.requires_human_review is True
        assert (
            event.change_note is not None
            and "conflicting with the recorded 2026-09-01" in event.change_note
        )

    def test_new_content_sends_a_human_approved_record_back_for_review(
        self, session: Session, source: Source
    ) -> None:
        """An approval covered the content a human saw, not what arrives afterwards."""
        first = run(
            session, source, FIRST, factories.make_result(severity="severe", confidence=0.95)
        )
        event = session.get(Event, first.event_id)
        assert event is not None
        apply_decision(
            session,
            event,
            reviewer="analyst",
            decision="edit",
            reason="72h is moderate.",
            edits={"severity": "moderate"},
        )
        assert event.requires_human_review is False

        added = follow_up_result(
            quotes=[("summary", "Vessels are being held at Example Anchorage")]
        )
        assert added.event is not None
        added = added.model_copy(
            update={
                "event": added.event.model_copy(
                    update={
                        "locations": [
                            ExtractedLocation(
                                name="Example Anchorage", location_type="port", precision="facility"
                            )
                        ]
                    }
                )
            }
        )
        run(session, source, FOLLOW_UP, added)

        assert event.requires_human_review is True
        assert event.review_status == "pending"


class TestProductLevel:
    def test_the_feed_shows_one_event_for_two_reports(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        run(session, source, FIRST, factories.make_result())
        run(session, source, FOLLOW_UP, follow_up_result())

        assert client.get("/events").json()["total"] == 1

    def test_the_event_page_cites_both_sources(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        first = run(session, source, FIRST, factories.make_result())
        run(session, source, FOLLOW_UP, follow_up_result())

        body = client.get(f"/events/{first.event_id}").json()
        assert len(body["source_links"]) == 2


def test_each_quote_says_which_document_it_came_from(
    client: TestClient, session: Session, source: Source
) -> None:
    """One publisher often issues several notices a day; the source name alone cannot
    tell an original from its update, so every quote names its document."""
    first = run(session, source, FIRST, factories.make_result())
    follow_up = follow_up_result()
    # Give the second document a distinguishable title.
    second_doc_title = "Update to the Example Port notice"
    run_result = run(session, source, FOLLOW_UP, follow_up)
    doc = session.scalar(
        select(RawDocument)
        .join(EventDocument, EventDocument.document_id == RawDocument.id)
        .where(EventDocument.relation == run_result.relation)
    )
    assert doc is not None
    doc.title = second_doc_title
    session.flush()

    evidence = client.get(f"/events/{first.event_id}").json()["evidence"]
    assert {e["document_title"] for e in evidence} == {"Example Port notice", second_doc_title}
    assert second_doc_title in client.get(f"/ui/events/{first.event_id}").text


def test_every_extraction_points_at_the_prompt_version_it_used(
    session: Session, source: Source
) -> None:
    """ADR-0001 D6: prompt_id and prompt_version must name the same template."""
    run(session, source, FIRST, factories.make_result())

    rows = session.execute(
        select(Extraction, Prompt).join(Prompt, Extraction.prompt_id == Prompt.id)
    ).all()
    assert rows
    for extraction, prompt in rows:
        assert extraction.prompt_version == prompt.version
