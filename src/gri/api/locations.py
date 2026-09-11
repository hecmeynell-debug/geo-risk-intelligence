"""The aggregate location view.

NG-4 shapes everything here. Locations are fixed features and administrative areas --
ports, straits, terminals, corridors, countries -- counted across events. There are no
coordinates in the response, no per-event positions, and nothing ordered in time: the
view answers "where has disruption been reported", never "where is something now".

A map is deliberately not drawn. The pipeline does not geocode (``event_locations``
latitude and longitude are never set), and adding a geocoder would mean a new external
dependency for a picture the counts already convey. ADR-0003 records the trade-off.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gri.db import get_db
from gri.models import Event, EventLocation

router = APIRouter(tags=["locations"])

SCOPE_NOTE = (
    "Counts of established records per fixed feature or administrative area. No"
    " coordinates, no individual positions, and no movement over time are recorded or"
    " served."
)


class LocationCount(BaseModel):
    name: str
    location_type: str
    country_code: str | None
    events: int


class CountryCount(BaseModel):
    country_code: str | None
    events: int


class LocationSummary(BaseModel):
    scope_note: str
    include_pending_review: bool
    by_location: list[LocationCount]
    by_country: list[CountryCount]


def location_summary(db: Session, include_pending_review: bool = False) -> LocationSummary:
    base = select(EventLocation).join(Event, EventLocation.event_id == Event.event_id)
    base = base.where(Event.review_status != "rejected")
    if not include_pending_review:
        base = base.where(Event.requires_human_review.is_(False))
    rows = base.subquery()

    by_location = db.execute(
        select(
            rows.c.name,
            rows.c.location_type,
            rows.c.country_code,
            func.count(func.distinct(rows.c.event_id)).label("events"),
        )
        .group_by(rows.c.name, rows.c.location_type, rows.c.country_code)
        .order_by(func.count(func.distinct(rows.c.event_id)).desc(), rows.c.name)
    ).all()

    by_country = db.execute(
        select(rows.c.country_code, func.count(func.distinct(rows.c.event_id)).label("events"))
        .group_by(rows.c.country_code)
        .order_by(func.count(func.distinct(rows.c.event_id)).desc(), rows.c.country_code)
    ).all()

    return LocationSummary(
        scope_note=SCOPE_NOTE,
        include_pending_review=include_pending_review,
        by_location=[
            LocationCount(
                name=r.name,
                location_type=r.location_type,
                country_code=r.country_code,
                events=r.events,
            )
            for r in by_location
        ],
        by_country=[CountryCount(country_code=r.country_code, events=r.events) for r in by_country],
    )


@router.get(
    "/locations",
    response_model=LocationSummary,
    summary="Aggregate counts per location (fixed features only)",
)
def get_locations(
    db: Annotated[Session, Depends(get_db)],
    include_pending_review: Annotated[bool, Query()] = False,
) -> LocationSummary:
    return location_summary(db, include_pending_review)
