"""The daily briefing: assembled from verified evidence, never generated.

A briefing is the most quotable thing this system produces, which makes it the easiest
place to break NG-3 and NG-5. So it is built with no model call at all -- every line in
it is either a field of an established record or a verbatim, verified quote with a link
to its source. ``tests/test_briefing.py`` asserts this module imports no model provider,
and that the same day briefs byte-identically twice.

What a briefing leaves out is reported, not hidden:

* Records flagged for human review are withheld -- CONSTRAINTS.md Section 6 says they
  are not established events -- but the briefing *counts* them, so a partial briefing is
  visibly partial.
* That count is split out for records a human has **approved** but which still carry the
  review flag because the database requires it (``high``/``severe`` severity, or low
  confidence). Under the current rule those can never appear in a briefing. Surfacing the
  number keeps that consequence in view instead of letting it pass silently -- see
  ADR-0003 D4, which leaves the rule for a human to decide.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from gri.models import Event, EventEvidence, RawDocument, Source
from gri.taxonomy import SEVERITY_LEVELS

#: Most quotes shown per record. Enough to evidence the headline fields without turning
#: the briefing into a document dump.
MAX_QUOTES_PER_ITEM = 3

_SEVERITY_RANK = {level: rank for rank, level in enumerate(SEVERITY_LEVELS)}

DISCLAIMER = (
    "This briefing lists records assembled from public and official sources. Every line"
    " is a field of an established record or a verbatim quote verified against the text"
    " retrieved at ingestion time. It contains no generated analysis. Coverage is not"
    " comprehensive, and the absence of an event here is not evidence that nothing"
    " happened."
)


@dataclass(frozen=True)
class BriefingQuote:
    field_supported: str
    quote: str
    source_name: str
    source_url: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class BriefingItem:
    event_id: uuid.UUID
    kind: str  # "new" or "updated"
    title: str
    event_type: str
    event_date: date
    severity: str
    confidence: float | None
    locations: list[str]
    sectors: list[str]
    quotes: list[BriefingQuote]
    change_note: str | None


@dataclass
class Briefing:
    day: date
    generated_from: str = "established records and verified quotes only; no model call"
    items: list[BriefingItem] = field(default_factory=list)
    #: Flagged records in the window that are awaiting a human decision.
    withheld_pending_review: int = 0
    #: Records a human approved that still carry the flag the database requires.
    withheld_approved_but_flagged: int = 0
    #: Established records dropped because they had no verified quote. Should be zero;
    #: reported because a briefing line without evidence must never be served.
    excluded_no_verified_evidence: int = 0
    disclaimer: str = DISCLAIMER

    @property
    def is_partial(self) -> bool:
        return bool(self.withheld_pending_review or self.withheld_approved_but_flagged)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _in_window(value: datetime | None, start: datetime, end: datetime) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return start <= value < end


def build_briefing(session: Session, day: date) -> Briefing:
    """Every record first seen or updated on ``day`` (UTC), with its verified evidence."""
    start, end = _day_bounds(day)
    briefing = Briefing(day=day)

    candidates = list(
        session.scalars(
            select(Event)
            .options(selectinload(Event.locations), selectinload(Event.sectors))
            .where(
                or_(
                    (Event.first_seen_at >= start) & (Event.first_seen_at < end),
                    (Event.last_updated_at >= start) & (Event.last_updated_at < end),
                )
            )
            .where(Event.review_status != "rejected")
        )
    )

    established: list[Event] = []
    for event in candidates:
        if not event.requires_human_review:
            established.append(event)
        elif event.review_status == "approved":
            briefing.withheld_approved_but_flagged += 1
        else:
            briefing.withheld_pending_review += 1

    for event in established:
        quotes = _verified_quotes(session, event.event_id)
        if not quotes:
            briefing.excluded_no_verified_evidence += 1
            continue
        briefing.items.append(
            BriefingItem(
                event_id=event.event_id,
                kind="new" if _in_window(event.first_seen_at, start, end) else "updated",
                title=event.title,
                event_type=event.event_type,
                event_date=event.event_date,
                severity=event.severity,
                confidence=float(event.confidence) if event.confidence is not None else None,
                locations=sorted(loc.name for loc in event.locations),
                sectors=sorted(s.sector for s in event.sectors),
                quotes=quotes,
                change_note=event.change_note,
            )
        )

    # Deterministic: most severe first, then by date, then by title, then by id, so the
    # same inputs always produce the same briefing.
    briefing.items.sort(
        key=lambda item: (
            -_SEVERITY_RANK.get(item.severity, 0),
            item.event_date,
            item.title,
            str(item.event_id),
        )
    )
    return briefing


#: Which quotes a briefing shows first. A reader skimming a briefing needs the quote
#: that says *what happened* before the one that says who issued the notice; ordering
#: alphabetically by field name led with "actors", the least informative of the lot.
#: Unlisted fields sort after these, alphabetically, so the order stays deterministic.
QUOTE_FIELD_PRIORITY: tuple[str, ...] = (
    "summary",
    "event_type",
    "severity",
    "event_date",
    "locations",
    "affected_sectors",
    "actors",
    "title",
)


def _quote_rank(field_supported: str) -> tuple[int, str]:
    try:
        return QUOTE_FIELD_PRIORITY.index(field_supported), ""
    except ValueError:
        return len(QUOTE_FIELD_PRIORITY), field_supported


def _verified_quotes(session: Session, event_id: uuid.UUID) -> list[BriefingQuote]:
    rows = session.execute(
        select(EventEvidence, RawDocument, Source)
        .join(RawDocument, EventEvidence.document_id == RawDocument.id)
        .join(Source, RawDocument.source_id == Source.id)
        .where(EventEvidence.event_id == event_id)
        .where(EventEvidence.verified.is_(True))
    ).all()
    rows = sorted(rows, key=lambda r: (_quote_rank(r[0].field_supported), r[0].quote))

    return [
        BriefingQuote(
            field_supported=ev.field_supported,
            quote=ev.quote,
            source_name=src.name,
            source_url=doc.canonical_url or doc.url,
            published_at=doc.published_at,
        )
        for ev, doc, src in rows[:MAX_QUOTES_PER_ITEM]
    ]
