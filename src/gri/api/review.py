"""The human review queue: list what needs a human, and record what they decide.

There is no authentication in this project -- it is a local tool -- so ``reviewer`` is
a self-declared name. ADR-0003 records that as a limitation: the review endpoints must
not be exposed beyond a trusted network.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from gri.api.events import EventSummaryOut, _evidence_counts, _summary_out
from gri.db import get_db
from gri.models import Event
from gri.review import ReviewError, apply_decision, decision_history, review_reasons

router = APIRouter(prefix="/review", tags=["review"])


class ReviewItemOut(BaseModel):
    event: EventSummaryOut
    #: Why this record is in the queue, derived from its current fields.
    reasons: list[str]


class ReviewQueue(BaseModel):
    total: int
    items: list[ReviewItemOut]


class DecisionIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    decision: Literal["approve", "reject", "edit"]
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="Mandatory. A decision without a reason is not auditable.",
    )
    edits: dict[str, Any] | None = Field(
        default=None,
        description="Only with decision 'edit'. Editable: title, event_type, event_date, severity.",
    )


class DecisionOut(BaseModel):
    decision_id: uuid.UUID
    event_id: uuid.UUID
    decision: str
    review_status: str
    requires_human_review: bool
    changes: dict[str, dict[str, Any]]
    flag_kept_because: list[str]


class HistoryEntryOut(BaseModel):
    decision_id: uuid.UUID
    reviewer: str
    decision: str
    reason: str
    changes: dict[str, Any]
    decided_at: datetime


def pending_queue(db: Session, limit: int = 100) -> ReviewQueue:
    """Records awaiting a human, oldest first so nothing waits indefinitely."""
    statement = (
        select(Event)
        .options(selectinload(Event.locations), selectinload(Event.sectors))
        .where(Event.review_status == "pending")
        .where(Event.requires_human_review.is_(True))
    )
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    events = list(db.scalars(statement.order_by(Event.created_at).limit(limit)))
    counts = _evidence_counts(db, [e.event_id for e in events])
    return ReviewQueue(
        total=total,
        items=[
            ReviewItemOut(
                event=_summary_out(e, counts.get(e.event_id, 0)), reasons=review_reasons(e)
            )
            for e in events
        ],
    )


def record_decision(db: Session, event_id: uuid.UUID, payload: DecisionIn) -> DecisionOut:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "event not found")
    try:
        result = apply_decision(
            db,
            event,
            reviewer=payload.reviewer,
            decision=payload.decision,
            reason=payload.reason,
            edits=payload.edits,
        )
    except ReviewError as exc:
        raise HTTPException(422, str(exc)) from exc
    return DecisionOut(**result.__dict__)


@router.get("", response_model=ReviewQueue, summary="Records awaiting human review")
def get_queue(
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ReviewQueue:
    return pending_queue(db, limit)


@router.post(
    "/{event_id}/decisions",
    response_model=DecisionOut,
    status_code=201,
    summary="Record a decision: approve, reject, or edit, with a mandatory reason",
)
def post_decision(
    event_id: uuid.UUID, payload: DecisionIn, db: Annotated[Session, Depends(get_db)]
) -> DecisionOut:
    return record_decision(db, event_id, payload)


@router.get(
    "/{event_id}/decisions",
    response_model=list[HistoryEntryOut],
    summary="Every decision on a record, oldest first",
)
def get_history(
    event_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]
) -> list[HistoryEntryOut]:
    if db.get(Event, event_id) is None:
        raise HTTPException(404, "event not found")
    return [
        HistoryEntryOut(
            decision_id=row.id,
            reviewer=row.reviewer,
            decision=row.decision,
            reason=row.reason,
            changes=row.changes,
            decided_at=row.decided_at,
        )
        for row in decision_history(db, event_id)
    ]
