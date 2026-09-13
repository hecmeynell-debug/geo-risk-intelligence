"""The dashboard: server-rendered HTML over the same objects the JSON API returns.

No JavaScript framework, no bundler, no build step (NG-7): templates ship inside the
package, so ``docker compose up --build`` stays one command.

Every page renders the API's own response objects rather than querying the database its
own way. That is the point of the design: "never serve an event without its evidence" and
"never present a flagged record as established" each have one code path, shared with the
API, instead of two that could drift.

Everything shown here that came from a source is untrusted text. Jinja2 autoescapes it,
and :func:`safe_url` refuses any link that is not ``http``/``https`` -- a ``javascript:``
URL in a hostile feed would otherwise execute when a reviewer clicked it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from gri.api.briefing import briefing_out
from gri.api.events import build_event_detail, list_events
from gri.api.locations import location_summary
from gri.api.review import DecisionIn, pending_queue, record_decision
from gri.briefing import build_briefing
from gri.db import get_db
from gri.models import Event
from gri.review import EDITABLE_FIELDS, decision_history, review_reasons
from gri.taxonomy import EVENT_TYPES, SEVERITY_LEVELS, sorted_values

router = APIRouter(prefix="/ui", tags=["dashboard"], include_in_schema=False)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def safe_url(value: object) -> str | None:
    """Return the URL only if it is http(s); anything else renders as no link at all."""
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value.strip()
    return None


templates.env.filters["safe_url"] = safe_url
templates.env.globals.update(
    event_types=sorted_values(EVENT_TYPES),
    severities=list(SEVERITY_LEVELS),
    editable_fields=sorted(EDITABLE_FIELDS),
)


def _page(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name=name, context=context)


def _message(exc: Exception) -> str:
    """A rejection message a reviewer can act on, whatever layer raised it."""
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "input"
        return f"{where}: {first.get('msg', 'invalid')}"
    return str(exc)


@router.get("", response_class=HTMLResponse, summary="Event feed")
def feed_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    event_type: Annotated[str | None, Query()] = None,
    severity: Annotated[str | None, Query()] = None,
    country: Annotated[str | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    # Empty form fields arrive as "" -- treat them as "no filter", not as a value.
    page = list_events(
        db=db,
        event_type=event_type or None,
        severity=severity or None,
        country=country or None,
        date_from=date_from,
        date_to=date_to,
        include_pending_review=False,
        limit=50,
        offset=offset,
    )
    queue = pending_queue(db, limit=1)
    return _page(
        request,
        "feed.html",
        {
            "page": page,
            "filters": {
                "event_type": event_type or "",
                "severity": severity or "",
                "country": country or "",
                "date_from": date_from.isoformat() if date_from else "",
                "date_to": date_to.isoformat() if date_to else "",
            },
            "pending_total": queue.total,
        },
    )


@router.get("/events/{event_id}", response_class=HTMLResponse, summary="One event")
def event_page(
    request: Request, event_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]
) -> HTMLResponse:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "event not found")
    return _page(
        request,
        "event.html",
        {
            "event": build_event_detail(db, event),
            "reasons": review_reasons(event) if event.requires_human_review else [],
            "history": decision_history(db, event_id),
        },
    )


@router.get("/locations", response_class=HTMLResponse, summary="Aggregate locations")
def locations_page(request: Request, db: Annotated[Session, Depends(get_db)]) -> HTMLResponse:
    return _page(request, "locations.html", {"summary": location_summary(db)})


@router.get("/briefing", response_class=HTMLResponse, summary="Daily briefing")
def briefing_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    day: Annotated[date | None, Query(alias="date")] = None,
) -> HTMLResponse:
    day = day or datetime.now(UTC).date()
    return _page(request, "briefing.html", {"briefing": briefing_out(build_briefing(db, day))})


@router.get("/review", response_class=HTMLResponse, summary="Review queue")
def review_page(request: Request, db: Annotated[Session, Depends(get_db)]) -> HTMLResponse:
    return _page(request, "review.html", {"queue": pending_queue(db, limit=200)})


def _review_context(db: Session, event_id: uuid.UUID) -> dict[str, Any]:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "event not found")
    return {
        "event": build_event_detail(db, event),
        "reasons": review_reasons(event),
        "history": decision_history(db, event_id),
        "error": None,
        "outcome": None,
        "form": {},
    }


@router.get("/review/{event_id}", response_class=HTMLResponse, summary="Review one record")
def review_item_page(
    request: Request, event_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]
) -> HTMLResponse:
    return _page(request, "review_detail.html", _review_context(db, event_id))


@router.post("/review/{event_id}", response_class=HTMLResponse, summary="Record a decision")
def review_submit(
    request: Request,
    event_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
    reviewer: Annotated[str, Form()] = "",
    decision: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
    edit_title: Annotated[str, Form()] = "",
    edit_event_type: Annotated[str, Form()] = "",
    edit_event_date: Annotated[str, Form()] = "",
    edit_severity: Annotated[str, Form()] = "",
) -> HTMLResponse:
    context = _review_context(db, event_id)
    form = {
        "reviewer": reviewer,
        "decision": decision,
        "reason": reason,
        "edit_title": edit_title,
        "edit_event_type": edit_event_type,
        "edit_event_date": edit_event_date,
        "edit_severity": edit_severity,
    }
    edits = None
    if decision == "edit":
        edits = {
            "title": edit_title,
            "event_type": edit_event_type,
            "event_date": edit_event_date,
            "severity": edit_severity,
        }

    try:
        payload = DecisionIn(reviewer=reviewer, decision=decision, reason=reason, edits=edits)
        outcome = record_decision(db, event_id, payload)
    except (ValidationError, HTTPException) as exc:
        # Redisplay with what the reviewer typed, so a rejected decision costs them a
        # correction rather than a retype. Nothing was recorded.
        context.update(error=_message(exc), form=form)
        return _page(request, "review_detail.html", context)

    refreshed = _review_context(db, event_id)
    refreshed["outcome"] = outcome
    return _page(request, "review_detail.html", refreshed)
