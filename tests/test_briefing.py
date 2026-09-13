"""The daily briefing.

Two properties matter more than any output format: nothing in a briefing is generated,
and nothing withheld goes unreported. The first module-level test runs without a
database; the rest are integration tests.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

import gri.api.briefing as briefing_api
import gri.briefing as briefing_module
from gri.api.briefing import briefing_out
from gri.briefing import build_briefing
from gri.models import EventEvidence, Source
from gri.review import apply_decision
from tests.helpers import DAY, seed_event

BRIEFING_DAY = DAY.date()


@pytest.mark.parametrize("module", [briefing_module, briefing_api])
def test_the_briefing_path_makes_no_model_call(module: object) -> None:
    """NG-5: a briefing is assembled from cited records, never composed by a model.

    Checked on the source text, so a future import of the extraction provider fails here
    before it can put generated prose into the most quotable artefact the system makes.
    """
    source = inspect.getsource(module)  # type: ignore[arg-type]
    for forbidden in ("anthropic", "extract_document", "ExtractionProvider", "processing.extract"):
        assert forbidden not in source, f"{forbidden!r} must not appear in the briefing path"


pytestmark_integration = pytest.mark.integration


@pytestmark_integration
class TestWhatIsIncluded:
    def test_an_established_record_appears_with_verified_quotes_and_links(
        self, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="moderate", confidence=0.9)

        briefing = build_briefing(session, BRIEFING_DAY)

        assert [item.event_id for item in briefing.items] == [event.event_id]
        item = briefing.items[0]
        assert item.kind == "new"
        assert item.quotes
        for quote in item.quotes:
            assert quote.source_url and quote.source_url.startswith("https://")
            assert quote.quote

    def test_only_verified_quotes_are_shown(self, session: Session, source: Source) -> None:
        event = seed_event(session, source)
        session.execute(
            update(EventEvidence)
            .where(EventEvidence.event_id == event.event_id)
            .where(EventEvidence.field_supported == "severity")
            .values(verified=False)
        )

        quotes = build_briefing(session, BRIEFING_DAY).items[0].quotes
        assert quotes
        assert all(q.field_supported != "severity" for q in quotes)

    def test_a_record_with_no_verified_quote_is_excluded_and_counted(
        self, session: Session, source: Source
    ) -> None:
        """A briefing line without evidence must never be served."""
        event = seed_event(session, source)
        session.execute(
            update(EventEvidence)
            .where(EventEvidence.event_id == event.event_id)
            .values(verified=False)
        )

        briefing = build_briefing(session, BRIEFING_DAY)
        assert briefing.items == []
        assert briefing.excluded_no_verified_evidence == 1

    def test_the_most_informative_quote_comes_first(self, session: Session, source: Source) -> None:
        """A skimming reader needs 'what happened' before 'who issued the notice'.

        Alphabetical field order would put 'severity' ahead of 'summary'; the priority
        order leads with the summary quote.
        """
        seed_event(session, source)
        quotes = build_briefing(session, BRIEFING_DAY).items[0].quotes
        assert [q.field_supported for q in quotes] == ["summary", "severity"]

    def test_other_days_are_not_included(self, session: Session, source: Source) -> None:
        seed_event(session, source)
        assert build_briefing(session, date(2026, 9, 2)).items == []

    def test_an_update_on_the_day_is_marked_as_an_update(
        self, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, published_at=datetime(2026, 8, 30, 9, 0, tzinfo=UTC))
        event.last_updated_at = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        session.flush()

        item = build_briefing(session, BRIEFING_DAY).items[0]
        assert item.kind == "updated"


@pytestmark_integration
class TestWhatIsWithheld:
    def test_a_record_awaiting_review_is_withheld_and_counted(
        self, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="severe", confidence=0.95)

        briefing = build_briefing(session, BRIEFING_DAY)

        assert briefing.items == []
        assert briefing.withheld_pending_review == 1
        assert briefing.is_partial

    def test_an_approved_high_impact_record_is_counted_separately(
        self, session: Session, source: Source
    ) -> None:
        """The consequence ADR-0003 D4 leaves open: approved, but still flagged, so still
        withheld. Counted on its own so it is visible rather than silently missing."""
        event = seed_event(session, source, severity="severe", confidence=0.95)
        apply_decision(session, event, reviewer="analyst", decision="approve", reason="Verified.")

        briefing = build_briefing(session, BRIEFING_DAY)

        assert briefing.items == []
        assert briefing.withheld_approved_but_flagged == 1
        assert briefing.withheld_pending_review == 0

    def test_a_rejected_record_is_neither_shown_nor_counted(
        self, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)
        apply_decision(session, event, reviewer="analyst", decision="reject", reason="Misread.")

        briefing = build_briefing(session, BRIEFING_DAY)

        assert briefing.items == []
        assert not briefing.is_partial


@pytestmark_integration
class TestDeterminism:
    def test_the_same_day_briefs_identically_twice(self, session: Session, source: Source) -> None:
        seed_event(session, source, severity="moderate")
        seed_event(session, source, severity="low")

        first = briefing_out(build_briefing(session, BRIEFING_DAY)).model_dump_json()
        second = briefing_out(build_briefing(session, BRIEFING_DAY)).model_dump_json()

        assert first == second

    def test_more_severe_records_come_first(self, session: Session, source: Source) -> None:
        seed_event(session, source, severity="low")
        seed_event(session, source, severity="moderate")

        severities = [item.severity for item in build_briefing(session, BRIEFING_DAY).items]
        assert severities == ["moderate", "low"]


@pytestmark_integration
def test_the_briefing_restates_its_limits(session: Session, source: Source) -> None:
    seed_event(session, source)
    disclaimer = build_briefing(session, BRIEFING_DAY).disclaimer.lower()
    assert "no generated analysis" in disclaimer
    assert "not comprehensive" in disclaimer
