"""The review path's domain rules, against a live database.

Run with:  pytest -m integration
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from gri.models import Event, ReviewDecision, Source
from gri.review import ReviewError, apply_decision, decision_history, review_reasons
from tests.helpers import seed_event

pytestmark = pytest.mark.integration


def _flag_manually(session: Session, event: Event) -> Event:
    """Simulate a flag the database does NOT enforce (e.g. a future conflicting-sources
    trigger), so we can test that approval clears a flag when it is allowed to."""
    event.requires_human_review = True
    event.review_status = "pending"
    session.flush()
    return event


def _decisions(session: Session, event: Event) -> list[ReviewDecision]:
    return list(
        session.scalars(select(ReviewDecision).where(ReviewDecision.event_id == event.event_id))
    )


class TestApprove:
    def test_approval_clears_a_flag_the_database_does_not_require(
        self, session: Session, source: Source
    ) -> None:
        event = _flag_manually(
            session, seed_event(session, source, severity="moderate", confidence=0.9)
        )

        result = apply_decision(
            session, event, reviewer="analyst", decision="approve", reason="Quotes check out."
        )

        assert result.review_status == "approved"
        assert result.requires_human_review is False
        assert result.flag_kept_because == []
        assert event.requires_human_review is False

    @pytest.mark.parametrize("severity", ["high", "severe"])
    def test_approval_cannot_clear_the_flag_on_a_high_impact_record(
        self, session: Session, source: Source, severity: str
    ) -> None:
        """CONSTRAINTS.md trigger 4 is a database CHECK; a human approval cannot override it."""
        event = seed_event(session, source, severity=severity, confidence=0.95)
        assert event.requires_human_review

        result = apply_decision(
            session, event, reviewer="analyst", decision="approve", reason="Verified."
        )

        assert result.review_status == "approved"
        assert result.requires_human_review is True
        assert any("ck_events_high_severity_requires_review" in k for k in result.flag_kept_because)
        session.flush()  # the database agrees

    def test_approval_cannot_clear_the_flag_on_a_low_confidence_record(
        self, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="moderate", confidence=0.4)
        assert event.requires_human_review

        result = apply_decision(
            session, event, reviewer="analyst", decision="approve", reason="Fine."
        )

        assert result.requires_human_review is True
        assert any(
            "ck_events_low_confidence_requires_review" in k for k in result.flag_kept_because
        )


class TestReject:
    def test_rejection_keeps_the_flag_so_the_record_never_resurfaces(
        self, session: Session, source: Source
    ) -> None:
        event = _flag_manually(session, seed_event(session, source))

        result = apply_decision(
            session, event, reviewer="analyst", decision="reject", reason="Misread notice."
        )

        assert result.review_status == "rejected"
        assert result.requires_human_review is True
        assert result.flag_kept_because == []


class TestEdit:
    def test_lowering_severity_can_clear_the_flag_and_records_before_and_after(
        self, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)

        result = apply_decision(
            session,
            event,
            reviewer="analyst",
            decision="edit",
            reason="Source reports a 72-hour closure, which is moderate.",
            edits={"severity": "moderate"},
        )

        assert result.review_status == "edited"
        assert result.requires_human_review is False
        assert result.changes == {"severity": {"before": "severe", "after": "moderate"}}
        # The model's rationale argued for the old level; it must not survive the edit.
        assert event.severity_rationale is not None
        assert "Revised by reviewer analyst" in event.severity_rationale

    def test_raising_severity_to_high_keeps_the_flag(
        self, session: Session, source: Source
    ) -> None:
        event = _flag_manually(
            session, seed_event(session, source, severity="moderate", confidence=0.9)
        )

        result = apply_decision(
            session,
            event,
            reviewer="analyst",
            decision="edit",
            reason="Corridor closure.",
            edits={"severity": "high"},
        )

        assert result.requires_human_review is True
        session.flush()

    def test_editable_fields_are_applied(self, session: Session, source: Source) -> None:
        event = _flag_manually(session, seed_event(session, source))

        apply_decision(
            session,
            event,
            reviewer="analyst",
            decision="edit",
            reason="Clarify title and date.",
            edits={
                "title": "Example Port closed to all traffic",
                "event_date": "2026-09-02",
                "event_type": "port_disruption",
            },
        )

        assert event.title == "Example Port closed to all traffic"
        assert event.event_date == date(2026, 9, 2)

    def test_blank_edit_fields_mean_unchanged(self, session: Session, source: Source) -> None:
        event = _flag_manually(session, seed_event(session, source))
        before = event.title

        apply_decision(
            session,
            event,
            reviewer="a",
            decision="edit",
            reason="r",
            edits={"title": "", "severity": "low"},
        )

        assert event.title == before
        assert event.severity == "low"


class TestRefusals:
    """Every refusal must leave the record untouched and write no decision row."""

    @pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
    def test_a_decision_without_a_reason_is_refused(
        self, session: Session, source: Source, reason: str
    ) -> None:
        event = _flag_manually(session, seed_event(session, source))
        with pytest.raises(ReviewError, match="reason must not be blank"):
            apply_decision(session, event, reviewer="analyst", decision="approve", reason=reason)
        assert event.review_status == "pending"
        assert _decisions(session, event) == []

    def test_an_anonymous_decision_is_refused(self, session: Session, source: Source) -> None:
        event = _flag_manually(session, seed_event(session, source))
        with pytest.raises(ReviewError, match="reviewer must not be blank"):
            apply_decision(session, event, reviewer="  ", decision="approve", reason="ok")

    def test_an_unknown_decision_is_refused(self, session: Session, source: Source) -> None:
        event = _flag_manually(session, seed_event(session, source))
        with pytest.raises(ReviewError, match="decision must be one of"):
            apply_decision(session, event, reviewer="a", decision="escalate", reason="r")

    def test_the_summary_cannot_be_rewritten(self, session: Session, source: Source) -> None:
        """NG-5: a rewritten summary would be prose no source supports."""
        event = _flag_manually(session, seed_event(session, source))
        with pytest.raises(ReviewError, match="summary is not editable"):
            apply_decision(
                session,
                event,
                reviewer="a",
                decision="edit",
                reason="r",
                edits={"summary": "My own words."},
            )

    def test_an_event_type_outside_the_taxonomy_is_refused(
        self, session: Session, source: Source
    ) -> None:
        event = _flag_manually(session, seed_event(session, source))
        with pytest.raises(ReviewError, match="closed taxonomy"):
            apply_decision(
                session,
                event,
                reviewer="a",
                decision="edit",
                reason="r",
                edits={"event_type": "geopolitics"},
            )

    def test_a_bad_date_is_refused(self, session: Session, source: Source) -> None:
        event = _flag_manually(session, seed_event(session, source))
        with pytest.raises(ReviewError, match="ISO date"):
            apply_decision(
                session,
                event,
                reviewer="a",
                decision="edit",
                reason="r",
                edits={"event_date": "yesterday"},
            )

    def test_a_rejected_edit_leaves_nothing_half_applied(
        self, session: Session, source: Source
    ) -> None:
        """A valid field followed by an invalid one must not leave the valid one written."""
        event = _flag_manually(session, seed_event(session, source))
        title_before = event.title

        with pytest.raises(ReviewError):
            apply_decision(
                session,
                event,
                reviewer="a",
                decision="edit",
                reason="r",
                edits={"title": "A perfectly valid new title", "severity": "catastrophic"},
            )

        assert event.title == title_before
        assert event.review_status == "pending"
        assert _decisions(session, event) == []

    def test_an_edit_that_changes_nothing_is_refused(
        self, session: Session, source: Source
    ) -> None:
        event = _flag_manually(session, seed_event(session, source))
        with pytest.raises(ReviewError, match="use approve"):
            apply_decision(
                session,
                event,
                reviewer="a",
                decision="edit",
                reason="r",
                edits={"severity": event.severity},
            )

    def test_edits_are_refused_on_an_approval(self, session: Session, source: Source) -> None:
        event = _flag_manually(session, seed_event(session, source))
        with pytest.raises(ReviewError, match="only accepted with decision 'edit'"):
            apply_decision(
                session,
                event,
                reviewer="a",
                decision="approve",
                reason="r",
                edits={"severity": "low"},
            )


class TestHistoryAndReasons:
    def test_decisions_are_appended_in_order(self, session: Session, source: Source) -> None:
        event = _flag_manually(session, seed_event(session, source))
        apply_decision(session, event, reviewer="first", decision="reject", reason="Looked wrong.")
        apply_decision(
            session, event, reviewer="second", decision="approve", reason="Second look: fine."
        )

        history = decision_history(session, event.event_id)

        assert [h.reviewer for h in history] == ["first", "second"]
        assert all(h.reason for h in history)

    def test_reasons_explain_a_high_impact_flag(self, session: Session, source: Source) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)
        assert any("trigger 4" in r for r in review_reasons(event))

    def test_reasons_explain_a_low_confidence_flag(self, session: Session, source: Source) -> None:
        event = seed_event(session, source, severity="moderate", confidence=0.4)
        assert any("trigger 1" in r for r in review_reasons(event))
