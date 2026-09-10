"""Builders for extraction results used across the Phase 2 tests.

All synthetic. Quotes are drawn from :data:`NOTICE_TEXT` so that verification genuinely
passes or fails for the reason a test intends, rather than by accident.
"""

from __future__ import annotations

from datetime import date

from gri.ingestion.normalise import normalise_text
from gri.schemas import (
    EvidenceQuote,
    ExtractedActor,
    ExtractedEvent,
    ExtractedLocation,
    ExtractionResult,
)

NOTICE_TEXT = normalise_text(
    "The port of Example is closed to all traffic following a channel obstruction. "
    "Vessels should expect delays of up to 72 hours. "
    "The Example Port Authority will issue a further update at 0600 local time. "
    "Container and bulk operations are suspended until the channel is surveyed."
)

SECOND_NOTICE_TEXT = normalise_text(
    "Example Port remains closed following the channel obstruction reported yesterday. "
    "The Example Port Authority now expects delays of up to 96 hours. "
    "A survey vessel is on scene."
)

UNRELATED_NOTICE_TEXT = normalise_text(
    "Scheduled maintenance on the Northern Grid interconnector will reduce transfer "
    "capacity by 400 MW between 3 and 5 October. Operators have been notified."
)


def make_event(
    *,
    severity: str = "moderate",
    confidence: float = 0.9,
    quotes: list[tuple[str, str]] | None = None,
    event_type: str = "port_disruption",
    event_date: date = date(2026, 9, 1),
    with_locations: bool = True,
    with_actors: bool = True,
    with_sectors: bool = True,
) -> ExtractedEvent:
    """A well-formed event whose quotes are real passages from NOTICE_TEXT."""
    if quotes is None:
        quotes = [
            ("summary", "closed to all traffic following a channel obstruction"),
            ("severity", "delays of up to 72 hours"),
        ]

    return ExtractedEvent(
        event_type=event_type,
        event_date=event_date,
        title="Example Port closed after channel obstruction",
        summary=(
            "The notice reports that Example Port is closed to all traffic following a "
            "channel obstruction, with delays of up to 72 hours expected."
        ),
        severity=severity,
        severity_rationale="The source reports a closure with delays of up to 72 hours.",
        locations=(
            [
                ExtractedLocation(
                    name="Example Port",
                    location_type="port",
                    country_code="GB",
                    precision="facility",
                )
            ]
            if with_locations
            else []
        ),
        actors=(
            [
                ExtractedActor(
                    name="Example Port Authority",
                    entity_type="port_authority",
                    role="authority_issuing_notice",
                )
            ]
            if with_actors
            else []
        ),
        affected_sectors=["ports_terminals", "maritime_shipping"] if with_sectors else [],
        evidence=[EvidenceQuote(field_supported=f, quote=q) for f, q in quotes],
        confidence=confidence,
    )


def make_result(**kwargs: object) -> ExtractionResult:
    """An in-niche result carrying an event."""
    return ExtractionResult(
        in_niche=True,
        abstained=False,
        abstention_reason=None,
        event=make_event(**kwargs),  # type: ignore[arg-type]
    )


def make_abstention(
    reason: str = "The notice is too thin to support a record.",
) -> ExtractionResult:
    return ExtractionResult(in_niche=True, abstained=True, abstention_reason=reason, event=None)


def make_out_of_niche(
    reason: str = "This is a political statement with no reported operational effect.",
) -> ExtractionResult:
    return ExtractionResult(in_niche=False, abstained=False, abstention_reason=reason, event=None)


def make_result_with_fabricated_quote() -> ExtractionResult:
    """Looks perfect, cites something the document never said."""
    return make_result(
        quotes=[
            ("summary", "closed to all traffic following a channel obstruction"),
            ("severity", "the port will remain shut for at least three weeks"),
        ]
    )


def make_grid_result() -> ExtractionResult:
    """A result whose quotes come from UNRELATED_NOTICE_TEXT.

    Needed because quotes are verified against the document they are attached to: reusing
    the maritime factory against the grid notice fails verification, correctly.
    """
    return ExtractionResult(
        in_niche=True,
        abstained=False,
        abstention_reason=None,
        event=ExtractedEvent(
            event_type="power_grid_disruption",
            event_date=date(2026, 10, 3),
            title="Northern Grid interconnector capacity reduced for maintenance",
            summary=(
                "The notice reports scheduled maintenance on the Northern Grid "
                "interconnector, reducing transfer capacity by 400 MW between 3 and 5 "
                "October."
            ),
            severity="moderate",
            severity_rationale="The source reports a measurable 400 MW capacity reduction.",
            locations=[
                ExtractedLocation(
                    name="Northern Grid interconnector",
                    location_type="grid_region",
                    country_code=None,
                    precision="approximate",
                )
            ],
            actors=[],
            affected_sectors=["electricity"],
            evidence=[
                EvidenceQuote(
                    field_supported="summary",
                    quote="Scheduled maintenance on the Northern Grid interconnector",
                ),
                EvidenceQuote(
                    field_supported="severity",
                    quote="reduce transfer capacity by 400 MW",
                ),
            ],
            confidence=0.88,
        ),
    )
