"""The daily briefing endpoint. The assembly lives in :mod:`gri.briefing`."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from gri.briefing import Briefing, build_briefing
from gri.db import get_db

router = APIRouter(tags=["briefing"])


class BriefingQuoteOut(BaseModel):
    field_supported: str
    quote: str
    source_name: str
    source_url: str | None
    published_at: datetime | None


class BriefingItemOut(BaseModel):
    event_id: uuid.UUID
    kind: str
    title: str
    event_type: str
    event_date: date
    severity: str
    confidence: float | None
    locations: list[str]
    sectors: list[str]
    quotes: list[BriefingQuoteOut]
    change_note: str | None


class BriefingOut(BaseModel):
    day: date
    generated_from: str
    is_partial: bool
    withheld_pending_review: int
    withheld_approved_but_flagged: int
    excluded_no_verified_evidence: int
    items: list[BriefingItemOut]
    disclaimer: str


def briefing_out(briefing: Briefing) -> BriefingOut:
    return BriefingOut(
        day=briefing.day,
        generated_from=briefing.generated_from,
        is_partial=briefing.is_partial,
        withheld_pending_review=briefing.withheld_pending_review,
        withheld_approved_but_flagged=briefing.withheld_approved_but_flagged,
        excluded_no_verified_evidence=briefing.excluded_no_verified_evidence,
        items=[
            BriefingItemOut(
                **{k: v for k, v in item.__dict__.items() if k != "quotes"},
                quotes=[BriefingQuoteOut(**q.__dict__) for q in item.quotes],
            )
            for item in briefing.items
        ],
        disclaimer=briefing.disclaimer,
    )


@router.get(
    "/briefing",
    response_model=BriefingOut,
    summary="Evidence-linked daily briefing",
    description=(
        "Records first seen or updated on the given UTC day, each with verified quotes and"
        " source links. Assembled, not generated: there is no model call in this path."
        " Records awaiting review are withheld and counted."
    ),
)
def get_briefing(
    db: Annotated[Session, Depends(get_db)],
    day: Annotated[
        date | None, Query(alias="date", description="UTC day; defaults to today")
    ] = None,
) -> BriefingOut:
    return briefing_out(build_briefing(db, day or datetime.now(UTC).date()))
