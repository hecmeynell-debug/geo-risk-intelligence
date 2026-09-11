"""Deciding what a new document means for an event that already exists.

ADR-0001 D7 and ADR-0004. When a verified document clusters with an existing event, it
is not a new event. It is one of:

* a **duplicate** -- another report of the same facts. Its verified quotes corroborate
  the record; nothing else changes.
* an **update** -- it reports something the record does not yet say. A change is
  applied only if a verified quote from the new document supports it, and the change
  note is assembled from that quote. A change nothing supports is not applied and the
  record goes to a human (CONSTRAINTS.md Section 6, trigger 6).
* a **conflict**, recorded as an update that changes nothing -- it contradicts the
  record on the event type or date. The record is left as it is and goes to a human
  (trigger 3).

This module is pure: it compares a snapshot of the existing event with the new
extraction and returns a decision. It touches no database, so the rules can be tested
directly, and the pipeline is the only place that writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from gri.ingestion.normalise import normalise_text
from gri.processing.verify import VerifiedQuote
from gri.schemas import ExtractedEvent

#: The model labels evidence by field, but not always with the canonical name.
_FIELD_ALIASES: dict[str, frozenset[str]] = {
    "severity": frozenset({"severity"}),
    "event_date": frozenset({"event_date", "date"}),
    "event_type": frozenset({"event_type", "type"}),
    "affected_sectors": frozenset({"affected_sectors", "sectors", "sector"}),
}


@dataclass(frozen=True)
class EventSnapshot:
    """The parts of an existing event that a new report is compared against."""

    event_type: str
    event_date: date
    severity: str
    location_names: frozenset[str]
    actor_names: frozenset[str]
    sectors: frozenset[str]


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: str
    after: str
    #: The verified quote from the new document supporting ``after``, if there is one.
    quote: str | None = None


@dataclass
class Consolidation:
    """What a new document does to an existing event."""

    #: Changes to apply. Each carries a verified quote.
    applied: list[FieldChange] = field(default_factory=list)
    #: Material fields the new document contradicts. Not applied.
    conflicts: list[FieldChange] = field(default_factory=list)
    #: Material changes the new document asserts with no verified quote. Not applied.
    ungrounded: list[FieldChange] = field(default_factory=list)
    new_locations: list[str] = field(default_factory=list)
    new_actors: list[str] = field(default_factory=list)
    new_sectors: list[str] = field(default_factory=list)

    @property
    def relation(self) -> str:
        """``update`` if the report says anything new or contradicts the record."""
        if self.applied or self.conflicts or self.ungrounded or self.additions:
            return "update"
        return "duplicate"

    @property
    def additions(self) -> bool:
        return bool(self.new_locations or self.new_actors or self.new_sectors)

    @property
    def needs_review(self) -> bool:
        """Trigger 3 (conflict) or trigger 6 (a change nothing supports)."""
        return bool(self.conflicts or self.ungrounded)

    @property
    def changes_content(self) -> bool:
        """Whether applying this alters what the record says."""
        return bool(self.applied or self.additions)


def _key(text: str) -> str:
    return normalise_text(text).casefold()


def _quote_for(quotes: list[VerifiedQuote], canonical: str) -> str | None:
    aliases = _FIELD_ALIASES.get(canonical, frozenset({canonical}))
    for quote in quotes:
        if quote.verified and quote.field_supported.strip().casefold() in aliases:
            return quote.quote
    return None


def _quote_mentioning(quotes: list[VerifiedQuote], name: str) -> str | None:
    """A verified quote from the new document that names ``name``.

    A location or organisation is added only if the new source's own verified text
    mentions it -- the field label a model attaches to a quote is not evidence that the
    quote supports it.
    """
    needle = _key(name)
    for quote in quotes:
        if quote.verified and needle and needle in _key(quote.quote):
            return quote.quote
    return None


def _grounded_additions(
    names: list[str], known: frozenset[str], quotes: list[VerifiedQuote]
) -> list[str]:
    """Names not already on the record that the new source's verified text mentions."""
    added: list[str] = []
    for name in names:
        if _key(name) in known or name in added:
            continue
        if _quote_mentioning(quotes, name):
            added.append(name)
    return added


def compare(
    existing: EventSnapshot, new: ExtractedEvent, quotes: list[VerifiedQuote]
) -> Consolidation:
    """Decide what ``new`` changes about ``existing``, grounded in ``quotes``."""
    result = Consolidation()

    # -- Conflicts: a later report rarely changes what kind of event this is or when
    # it began. A different value means the sources disagree, which is a human's call.
    if new.event_type != existing.event_type:
        result.conflicts.append(
            FieldChange(
                "event_type", existing.event_type, new.event_type, _quote_for(quotes, "event_type")
            )
        )
    if new.event_date != existing.event_date:
        result.conflicts.append(
            FieldChange(
                "event_date",
                existing.event_date.isoformat(),
                new.event_date.isoformat(),
                _quote_for(quotes, "event_date"),
            )
        )

    # -- Severity is expected to move as a disruption develops. Applied only if the new
    # document's verified text supports the new level.
    if new.severity != existing.severity:
        quote = _quote_for(quotes, "severity")
        change = FieldChange("severity", existing.severity, new.severity, quote)
        (result.applied if quote else result.ungrounded).append(change)

    # -- Additions: grounded in the new source's own text, or not made at all.
    result.new_locations = _grounded_additions(
        [loc.name for loc in new.locations], existing.location_names, quotes
    )
    result.new_actors = _grounded_additions(
        [actor.name for actor in new.actors], existing.actor_names, quotes
    )
    if _quote_for(quotes, "affected_sectors"):
        for sector in new.affected_sectors:
            if sector not in existing.sectors and sector not in result.new_sectors:
                result.new_sectors.append(sector)

    return result


def change_note(
    consolidation: Consolidation,
    source_name: str,
    published: date | None,
    quotes: list[VerifiedQuote],
) -> str | None:
    """Build the "what is new since the last update" note from fields and verbatim quotes.

    No sentence here is composed from the model's prose: each clause is a field change
    plus the quote that supports it, so the note is as traceable as the evidence (NG-5).
    """
    if consolidation.relation != "update":
        return None

    parts: list[str] = []
    for change in consolidation.applied:
        parts.append(
            f'{change.field} revised from {change.before} to {change.after}: "{change.quote}"'
        )
    for name in consolidation.new_locations:
        parts.append(f'location reported: {name}: "{_quote_mentioning(quotes, name)}"')
    for name in consolidation.new_actors:
        parts.append(f'organisation reported: {name}: "{_quote_mentioning(quotes, name)}"')
    if consolidation.new_sectors:
        parts.append(f"sectors added: {', '.join(consolidation.new_sectors)}")
    for change in consolidation.conflicts:
        support = f'"{change.quote}"' if change.quote else "no supporting quote"
        parts.append(
            f"{change.field} given as {change.after}, "
            f"conflicting with the recorded {change.before}: {support}"
        )
    for change in consolidation.ungrounded:
        parts.append(
            f"{change.field} given as {change.after}, "
            "but no verified quote supports it; not applied"
        )

    when = f" published {published.isoformat()}" if published else ""
    note = f"Update from {source_name}{when}: " + "; ".join(parts)
    # A quote usually ends with its own full stop; don't add a second after the quote mark.
    if not note.endswith(('."', ".")):
        note += "."
    if consolidation.needs_review:
        note += " Held for human review."
    return note
