"""The rules for folding a new report into an existing event. Pure: no database.

The property that matters most: nothing changes on a record unless a verified quote from
the new document supports the change.
"""

from __future__ import annotations

from datetime import date

from gri.processing.consolidate import EventSnapshot, change_note, compare
from gri.processing.verify import VerifiedQuote
from gri.schemas import ExtractedLocation
from tests import factories


def snapshot(**overrides: object) -> EventSnapshot:
    defaults: dict[str, object] = {
        "event_type": "port_disruption",
        "event_date": date(2026, 9, 1),
        "severity": "moderate",
        "location_names": frozenset({"example port"}),
        "actor_names": frozenset({"example port authority"}),
        "sectors": frozenset({"ports_terminals", "maritime_shipping"}),
    }
    defaults.update(overrides)
    return EventSnapshot(**defaults)  # type: ignore[arg-type]


def quote(field: str, text: str, verified: bool = True) -> VerifiedQuote:
    return VerifiedQuote(field_supported=field, quote=text, verified=verified)


class TestDuplicates:
    def test_the_same_facts_are_a_duplicate(self) -> None:
        new = factories.make_event()
        decision = compare(snapshot(), new, [quote("summary", "closed to all traffic")])

        assert decision.relation == "duplicate"
        assert not decision.changes_content
        assert not decision.needs_review

    def test_a_duplicate_writes_no_change_note(self) -> None:
        decision = compare(snapshot(), factories.make_event(), [])
        assert change_note(decision, "Source", date(2026, 9, 2), []) is None

    def test_a_known_location_in_different_case_is_not_new(self) -> None:
        new = factories.make_event().model_copy(
            update={
                "locations": [
                    ExtractedLocation(
                        name="EXAMPLE PORT", location_type="port", precision="facility"
                    )
                ]
            }
        )
        decision = compare(snapshot(), new, [quote("locations", "EXAMPLE PORT is closed")])
        assert decision.new_locations == []


class TestUpdates:
    def test_a_supported_severity_change_is_applied(self) -> None:
        new = factories.make_event(severity="high")
        support = quote("severity", "delays of up to ten days")

        decision = compare(snapshot(), new, [support])

        assert decision.relation == "update"
        assert [(c.field, c.before, c.after) for c in decision.applied] == [
            ("severity", "moderate", "high")
        ]
        assert decision.applied[0].quote == "delays of up to ten days"

    def test_an_unsupported_severity_change_is_not_applied(self) -> None:
        """Trigger 6: a change nothing supports goes to a human, not into the record."""
        new = factories.make_event(severity="high")

        decision = compare(snapshot(), new, [quote("summary", "the port remains closed")])

        assert decision.applied == []
        assert [c.field for c in decision.ungrounded] == ["severity"]
        assert decision.needs_review

    def test_an_unverified_quote_supports_nothing(self) -> None:
        new = factories.make_event(severity="high")
        decision = compare(snapshot(), new, [quote("severity", "ten days", verified=False)])
        assert decision.applied == []
        assert decision.ungrounded

    def test_a_new_location_named_in_the_new_text_is_added(self) -> None:
        new = factories.make_event().model_copy(
            update={
                "locations": [
                    ExtractedLocation(
                        name="Example Anchorage", location_type="port", precision="facility"
                    )
                ]
            }
        )
        decision = compare(
            snapshot(), new, [quote("summary", "vessels are held at Example Anchorage")]
        )
        assert decision.new_locations == ["Example Anchorage"]
        assert decision.relation == "update"

    def test_a_new_location_the_text_never_names_is_not_added(self) -> None:
        """A model can list a place; only the source's own words can add it."""
        new = factories.make_event().model_copy(
            update={
                "locations": [
                    ExtractedLocation(
                        name="Somewhere Else", location_type="port", precision="facility"
                    )
                ]
            }
        )
        decision = compare(snapshot(), new, [quote("locations", "the port is closed")])
        assert decision.new_locations == []
        assert decision.relation == "duplicate"


class TestConflicts:
    def test_a_different_date_is_a_conflict_not_a_change(self) -> None:
        """Trigger 3: sources disagree on a material field; a human decides."""
        new = factories.make_event(event_date=date(2026, 9, 5))

        decision = compare(snapshot(), new, [quote("date", "from 5 September")])

        assert [c.field for c in decision.conflicts] == ["event_date"]
        assert decision.applied == []
        assert decision.needs_review
        # The field-name alias "date" is recognised as supporting event_date.
        assert decision.conflicts[0].quote == "from 5 September"

    def test_a_different_event_type_is_a_conflict(self) -> None:
        new = factories.make_event(event_type="vessel_incident")
        decision = compare(snapshot(), new, [])
        assert [c.field for c in decision.conflicts] == ["event_type"]


class TestChangeNote:
    def test_the_note_carries_the_verbatim_quote(self) -> None:
        new = factories.make_event(severity="high")
        support = [quote("severity", "the closure is expected to last at least ten days")]
        decision = compare(snapshot(), new, support)

        note = change_note(decision, "Example Authority", date(2026, 9, 3), support)

        assert note is not None
        assert "severity revised from moderate to high" in note
        assert '"the closure is expected to last at least ten days"' in note
        assert "Example Authority published 2026-09-03" in note
        assert "Held for human review" not in note

    def test_a_quote_ending_in_a_full_stop_does_not_get_a_second_one(self) -> None:
        new = factories.make_event(severity="high")
        support = [quote("severity", "Vessels are waiting at the anchorage.")]
        note = change_note(compare(snapshot(), new, support), "Source", None, support)
        assert note is not None
        assert note.endswith('anchorage."')

    def test_a_conflict_is_described_and_held(self) -> None:
        new = factories.make_event(event_date=date(2026, 9, 5))
        decision = compare(snapshot(), new, [])

        note = change_note(decision, "Example Authority", None, [])

        assert note is not None
        assert "event_date given as 2026-09-05, conflicting with the recorded 2026-09-01" in note
        assert "no supporting quote" in note
        assert note.endswith("Held for human review.")
