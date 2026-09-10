"""Event and search endpoints.

Every event response carries its evidence: the quote, whether verification passed, and a
link to the source document it came from. That is not a nicety — an event served without
the text that supports it is exactly the unauditable assertion NG-3 and NG-5 forbid.

Records awaiting review are excluded by default and must be asked for explicitly, so a
caller cannot mistake a flagged record for an established one.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from gri.db import get_db
from gri.models import Event, EventEvidence, RawDocument, Source
from gri.taxonomy import EVENT_TYPES, SEVERITY_LEVELS, sorted_values

router = APIRouter(prefix="/events", tags=["events"])
search_router = APIRouter(tags=["search"])

MAX_PAGE_SIZE = 100


class EvidenceOut(BaseModel):
    """One quote, and whether it was actually found in the stored source text."""

    field_supported: str
    quote: str
    verified: bool
    verification_method: str | None
    source_url: str | None
    source_name: str | None
    published_at: str | None
    char_start: int | None
    char_end: int | None


class LocationOut(BaseModel):
    name: str
    location_type: str
    country_code: str | None
    precision: str | None


class ActorOut(BaseModel):
    name: str
    entity_type: str
    role: str


class EventSummaryOut(BaseModel):
    event_id: uuid.UUID
    event_type: str
    event_date: date
    title: str
    severity: str
    confidence: float | None
    requires_human_review: bool
    review_status: str
    status: str
    evidence_count: int
    locations: list[LocationOut]
    affected_sectors: list[str]


class EventDetailOut(EventSummaryOut):
    summary: str
    severity_rationale: str | None
    change_note: str | None
    actors: list[ActorOut]
    evidence: list[EvidenceOut]
    source_links: list[str]
    #: Restated on every detail response so a consumer cannot strip the context.
    disclaimer: str = Field(
        default=(
            "This record reflects what the cited sources reported. Quotes are verified "
            "against the text retrieved at ingestion time. Coverage is not comprehensive "
            "and absence of an event is not evidence that nothing happened."
        )
    )


class EventPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[EventSummaryOut]


def _evidence_out(session: Session, event: Event) -> list[EvidenceOut]:
    rows = session.execute(
        select(EventEvidence, RawDocument, Source)
        .join(RawDocument, EventEvidence.document_id == RawDocument.id)
        .join(Source, RawDocument.source_id == Source.id)
        .where(EventEvidence.event_id == event.event_id)
        .order_by(EventEvidence.field_supported)
    ).all()

    return [
        EvidenceOut(
            field_supported=ev.field_supported,
            quote=ev.quote,
            verified=ev.verified,
            verification_method=ev.verification_method,
            source_url=doc.canonical_url or doc.url,
            source_name=src.name,
            published_at=doc.published_at.isoformat() if doc.published_at else None,
            char_start=ev.quote_char_start,
            char_end=ev.quote_char_end,
        )
        for ev, doc, src in rows
    ]


def _summary_out(event: Event, evidence_count: int) -> EventSummaryOut:
    return EventSummaryOut(
        event_id=event.event_id,
        event_type=event.event_type,
        event_date=event.event_date,
        title=event.title,
        severity=event.severity,
        confidence=float(event.confidence) if event.confidence is not None else None,
        requires_human_review=event.requires_human_review,
        review_status=event.review_status,
        status=event.status,
        evidence_count=evidence_count,
        locations=[
            LocationOut(
                name=loc.name,
                location_type=loc.location_type,
                country_code=loc.country_code,
                precision=loc.location_precision,
            )
            for loc in event.locations
        ],
        affected_sectors=sorted(s.sector for s in event.sectors),
    )


@router.get("", response_model=EventPage, summary="List events")
def list_events(
    db: Annotated[Session, Depends(get_db)],
    event_type: Annotated[str | None, Query(description="Filter by event type")] = None,
    severity: Annotated[str | None, Query(description="Filter by severity")] = None,
    country: Annotated[str | None, Query(description="ISO alpha-2 country code")] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    include_pending_review: Annotated[
        bool,
        Query(
            description=(
                "Include records flagged for human review. Off by default so a flagged "
                "record is never mistaken for an established one."
            )
        ),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventPage:
    if event_type and event_type not in EVENT_TYPES:
        raise HTTPException(
            422, f"unknown event_type; expected one of {sorted_values(EVENT_TYPES)}"
        )
    if severity and severity not in SEVERITY_LEVELS:
        raise HTTPException(422, f"unknown severity; expected one of {list(SEVERITY_LEVELS)}")

    statement = select(Event).options(selectinload(Event.locations), selectinload(Event.sectors))
    if not include_pending_review:
        statement = statement.where(Event.requires_human_review.is_(False))
    if event_type:
        statement = statement.where(Event.event_type == event_type)
    if severity:
        statement = statement.where(Event.severity == severity)
    if date_from:
        statement = statement.where(Event.event_date >= date_from)
    if date_to:
        statement = statement.where(Event.event_date <= date_to)
    if country:
        statement = statement.where(
            Event.locations.any(func.upper(_location_country()) == country.upper())
        )

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    events = list(
        db.scalars(
            statement.order_by(Event.event_date.desc(), Event.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )

    counts = _evidence_counts(db, [e.event_id for e in events])
    return EventPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[_summary_out(e, counts.get(e.event_id, 0)) for e in events],
    )


def _location_country() -> Any:
    from gri.models import EventLocation

    return EventLocation.country_code


def _evidence_counts(db: Session, event_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not event_ids:
        return {}
    rows = db.execute(
        select(EventEvidence.event_id, func.count())
        .where(EventEvidence.event_id.in_(event_ids))
        .group_by(EventEvidence.event_id)
    ).all()
    return {row[0]: row[1] for row in rows}


@router.get("/{event_id}", response_model=EventDetailOut, summary="One event, with evidence")
def get_event(event_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]) -> EventDetailOut:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "event not found")

    evidence = _evidence_out(db, event)
    summary = _summary_out(event, len(evidence))

    return EventDetailOut(
        **summary.model_dump(),
        summary=event.summary,
        severity_rationale=event.severity_rationale,
        change_note=event.change_note,
        actors=[
            ActorOut(name=a.entity.canonical_name, entity_type=a.entity.entity_type, role=a.role)
            for a in event.actors
        ],
        evidence=evidence,
        source_links=sorted({e.source_url for e in evidence if e.source_url}),
    )


class SearchHit(BaseModel):
    event: EventSummaryOut
    matched_on: Literal["title", "summary", "evidence"]


class SearchResults(BaseModel):
    query: str
    total: int
    items: list[SearchHit]


@search_router.get("/search", response_model=SearchResults, summary="Search events")
def search_events(
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str, Query(min_length=2, max_length=200, description="Text to search for")],
    include_pending_review: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
) -> SearchResults:
    """Case-insensitive substring search over titles, summaries, and evidence quotes.

    Deliberately lexical rather than semantic. Semantic search over *events* would return
    records whose text never contains what the user asked for, which is confusing when the
    whole product promise is that you can see the supporting words. Vector similarity is
    used for clustering documents, where it belongs.
    """
    pattern = f"%{q}%"

    statement = (
        select(Event)
        .options(selectinload(Event.locations), selectinload(Event.sectors))
        .where(
            or_(
                Event.title.ilike(pattern),
                Event.summary.ilike(pattern),
                Event.evidence.any(EventEvidence.quote.ilike(pattern)),
            )
        )
    )
    if not include_pending_review:
        statement = statement.where(Event.requires_human_review.is_(False))

    events = list(db.scalars(statement.order_by(Event.event_date.desc()).limit(limit)))
    counts = _evidence_counts(db, [e.event_id for e in events])

    hits: list[SearchHit] = []
    needle = q.lower()
    for event in events:
        if needle in event.title.lower():
            matched: Literal["title", "summary", "evidence"] = "title"
        elif needle in event.summary.lower():
            matched = "summary"
        else:
            matched = "evidence"
        hits.append(
            SearchHit(event=_summary_out(event, counts.get(event.event_id, 0)), matched_on=matched)
        )

    return SearchResults(query=q, total=len(hits), items=hits)
