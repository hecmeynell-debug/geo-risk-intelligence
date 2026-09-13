"""FastAPI application.

Phase 2 added events and search; Phase 3 adds the briefing, the location view, the
review queue, and the server-rendered dashboard at /ui.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gri import __version__
from gri.api.briefing import router as briefing_router
from gri.api.events import router as events_router
from gri.api.events import search_router
from gri.api.locations import router as locations_router
from gri.api.review import router as review_router
from gri.api.ui import router as ui_router
from gri.config import Settings, get_settings
from gri.db import check_database, get_db

DESCRIPTION = """
Auditable, evidence-linked records of **maritime, energy, and supply-chain disruption**
reported by public and official sources.

Every event record is traceable to quotes verified against the source text we retrieved.
Records with weak or conflicting evidence are flagged for human review rather than
published as established.

This is an evidence-aggregation tool over public reporting. It is **not** an authoritative
security or intelligence product, its coverage is **not** comprehensive, and the absence
of an event here is **not** evidence that nothing happened. See `CONSTRAINTS.md`.
"""

router = APIRouter()


@router.get("/health", tags=["ops"], summary="Liveness and dependency check")
def health(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Report process health and the state of the database dependency.

    Returns 503 when the database is unreachable or pgvector is missing, so Compose and
    CI fail loudly rather than serving a broken stack.
    """
    payload: dict[str, Any] = {
        "status": "ok",
        "version": __version__,
        "environment": settings.environment,
        "database": None,
    }

    try:
        payload["database"] = check_database(db)
    except SQLAlchemyError as exc:
        payload["status"] = "degraded"
        payload["database"] = {"connected": False, "error": str(exc)}
        return JSONResponse(status_code=503, content=payload)

    if not payload["database"]["pgvector_installed"]:
        payload["status"] = "degraded"
        payload["database"]["error"] = "pgvector extension is not installed"
        return JSONResponse(status_code=503, content=payload)

    return JSONResponse(status_code=200, content=payload)


@router.get("/", tags=["ops"], summary="Service identity and stated limitations")
def root() -> dict[str, Any]:
    return {
        "name": "geo-risk-intelligence",
        "version": __version__,
        "scope": "maritime, energy, and supply-chain disruption events",
        "what_this_is": (
            "An evidence-aggregation tool over public and official reporting. Every "
            "claim is traceable to a verified quote from a stored source document."
        ),
        "what_this_is_not": (
            "Not an authoritative security or intelligence product, not a chatbot, and "
            "not a surveillance tool. It does not track or score individuals."
        ),
        "limitations": [
            "Coverage is limited to a small number of sources and is not comprehensive.",
            "Absence of an event here is not evidence that nothing happened.",
            "Records reflect what sources reported, including any source error or bias.",
            "Automated extraction makes mistakes; low-confidence records are flagged.",
        ],
        "docs": "/docs",
        "dashboard": "/ui",
    }


def create_app() -> FastAPI:
    app = FastAPI(
        title="geo-risk-intelligence",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=[
            {"name": "ops", "description": "Health and service identity."},
            {
                "name": "events",
                "description": (
                    "Evidence-linked disruption records. Every event carries the quotes"
                    " that support it and whether each was verified against the stored"
                    " source text. Records flagged for human review are excluded unless"
                    " explicitly requested."
                ),
            },
            {"name": "search", "description": "Lexical search over events and their evidence."},
            {
                "name": "briefing",
                "description": (
                    "Daily briefing assembled from established records and verified quotes."
                    " No model call. Records awaiting review are withheld and counted."
                ),
            },
            {
                "name": "locations",
                "description": "Aggregate counts per fixed feature. No coordinates or tracking.",
            },
            {
                "name": "review",
                "description": (
                    "The human review queue. Every decision needs a reason. There is no"
                    " authentication: do not expose these endpoints beyond a trusted network."
                ),
            },
        ],
    )
    app.include_router(router)
    app.include_router(events_router)
    app.include_router(search_router)
    app.include_router(briefing_router)
    app.include_router(locations_router)
    app.include_router(review_router)
    app.include_router(ui_router)
    return app


app = create_app()
