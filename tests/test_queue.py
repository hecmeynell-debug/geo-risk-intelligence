"""Automatic processing: what the worker picks up, and the guards on what it spends.

Run with:  pytest -m integration
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gri.ingestion.normalise import content_hash, sha256_text
from gri.models import Event, Extraction, Prompt, RawDocument, Source
from gri.processing.embed import DeterministicFakeProvider
from gri.processing.extract import ScriptedExtractionProvider
from gri.processing.queue import (
    MAX_ERROR_ROWS,
    ProcessingTick,
    pending_document_ids,
    process_pending,
    spent_today_usd,
)
from tests import factories

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def ingest(
    session: Session, source: Source, *, text: str | None = factories.NOTICE_TEXT
) -> RawDocument:
    """Store a document as ingestion would, without processing it."""
    marker = uuid.uuid4().hex
    body = f"{text} Ref {marker}." if text else None
    document = RawDocument(
        source_id=source.id,
        url=f"https://notices.example.invalid/{marker}",
        title="Example Port notice",
        published_at=NOW,
        retrieved_at=NOW,
        retrieval_method="rss",
        clean_text=body,
        raw_body=body,
        raw_hash=sha256_text(marker),
        content_hash=content_hash(marker),
    )
    session.add(document)
    session.flush()
    return document


def record_extraction(
    session: Session, document: RawDocument, outcome: str, cost: float = 0.0
) -> None:
    prompt = session.scalar(select(Prompt).limit(1))
    if prompt is None:
        prompt = Prompt(name="extract_event", version="test", template="t", template_hash="a" * 64)
        session.add(prompt)
        session.flush()
    session.add(
        Extraction(
            document_id=document.id,
            prompt_id=prompt.id,
            prompt_version="test",
            model_name="claude-sonnet-5",
            outcome=outcome,
            cost_usd=cost,
        )
    )
    session.flush()


def run(
    session: Session, *, limit: int = 10, budget: float = 10.0, results: int = 10
) -> ProcessingTick:
    return process_pending(
        session,
        limit=limit,
        daily_budget_usd=budget,
        embedding_provider=DeterministicFakeProvider(),
        extraction_provider=ScriptedExtractionProvider([factories.make_result()] * results),
    )


class TestWhatIsPending:
    def test_a_freshly_ingested_document_is_pending(self, session: Session, source: Source) -> None:
        document = ingest(session, source)
        assert pending_document_ids(session, 10) == [document.id]

    @pytest.mark.parametrize(
        "outcome", ["extracted", "abstained", "citation_invalid", "schema_invalid"]
    )
    def test_a_terminal_outcome_is_not_pending(
        self, session: Session, source: Source, outcome: str
    ) -> None:
        """Abstaining and failing citation are answers, not failures to retry."""
        document = ingest(session, source)
        record_extraction(session, document, outcome)
        assert pending_document_ids(session, 10) == []

    def test_an_errored_document_is_retried(self, session: Session, source: Source) -> None:
        document = ingest(session, source)
        record_extraction(session, document, "error")
        assert pending_document_ids(session, 10) == [document.id]

    def test_a_document_that_keeps_erroring_is_eventually_left_alone(
        self, session: Session, source: Source
    ) -> None:
        document = ingest(session, source)
        for _ in range(MAX_ERROR_ROWS):
            record_extraction(session, document, "error")
        assert pending_document_ids(session, 10) == []

    def test_a_metadata_only_document_is_never_processed(
        self, session: Session, source: Source
    ) -> None:
        """No stored text means no possible quote, so extracting it would buy nothing."""
        ingest(session, source, text=None)
        assert pending_document_ids(session, 10) == []

    def test_a_tombstoned_document_is_never_processed(
        self, session: Session, source: Source
    ) -> None:
        document = ingest(session, source)
        document.clean_text = None
        document.raw_body = None
        document.is_tombstoned = True
        session.flush()
        assert pending_document_ids(session, 10) == []


class TestProcessing:
    def test_pending_documents_become_events(self, session: Session, source: Source) -> None:
        ingest(session, source)
        ingest(session, source)

        tick = run(session)

        assert tick.processed == 2
        assert session.scalar(select(func.count()).select_from(Event)) == 2
        assert pending_document_ids(session, 10) == []

    def test_processing_is_idempotent_across_ticks(self, session: Session, source: Source) -> None:
        """A document already extracted must never be paid for twice."""
        ingest(session, source)
        run(session)
        second = run(session)

        assert second.processed == 0
        assert session.scalar(select(func.count()).select_from(Extraction)) == 1

    def test_the_per_tick_cap_is_respected(self, session: Session, source: Source) -> None:
        for _ in range(5):
            ingest(session, source)

        tick = run(session, limit=2)

        assert tick.processed == 2
        assert len(pending_document_ids(session, 10)) == 3

    @pytest.mark.parametrize("setting", [{"limit": 0}, {"budget": 0.0}])
    def test_zero_turns_processing_off(
        self, session: Session, source: Source, setting: dict[str, float]
    ) -> None:
        ingest(session, source)
        tick = run(session, **setting)  # type: ignore[arg-type]
        assert tick.processed == 0


class TestBudget:
    def test_processing_stops_when_the_daily_budget_is_spent(
        self, session: Session, source: Source
    ) -> None:
        already_paid = ingest(session, source)
        record_extraction(session, already_paid, "extracted", cost=1.50)
        waiting = ingest(session, source)

        tick = run(session, budget=1.00)

        assert tick.stopped_for_budget
        assert tick.processed == 0
        assert pending_document_ids(session, 10) == [waiting.id]

    def test_spend_is_measured_from_recorded_costs(self, session: Session, source: Source) -> None:
        first = ingest(session, source)
        second = ingest(session, source)
        record_extraction(session, first, "extracted", cost=0.25)
        record_extraction(session, second, "abstained", cost=0.10)
        assert spent_today_usd(session) == pytest.approx(0.35)

    def test_the_budget_is_checked_before_every_document(
        self, session: Session, source: Source
    ) -> None:
        """A tick must stop mid-way once spend crosses the line, not after the whole batch."""
        for _ in range(5):
            ingest(session, source)
        # Each scripted extraction records ~$0.0045 (1000 in, 250 out on Sonnet 5).
        tick = run(session, budget=0.009)

        assert tick.stopped_for_budget
        assert 1 <= tick.processed < 5


class TestFailureIsolation:
    def test_one_failing_document_does_not_stop_the_others(
        self, session: Session, source: Source
    ) -> None:
        ingest(session, source)
        ingest(session, source)

        class ExplodesOnce:
            def __init__(self) -> None:
                self.calls = 0
                self.inner = ScriptedExtractionProvider([factories.make_result()] * 5)

            def extract(self, system: str, user: str, model: str) -> object:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("provider blew up")
                return self.inner.extract(system, user, model)

        tick = process_pending(
            session,
            limit=10,
            daily_budget_usd=10.0,
            embedding_provider=DeterministicFakeProvider(),
            extraction_provider=ExplodesOnce(),  # type: ignore[arg-type]
        )

        assert tick.failed == 1
        assert tick.processed == 1
