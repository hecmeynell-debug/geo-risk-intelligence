"""Turning ingested documents into events, on the worker's schedule.

Until this existed, nothing outside the tests ever called ``process_document``: the worker
ingested documents and they sat in the raw store forever. This is the missing step
between ADR-0001's "raw document store" and "processing".

Because it spends real money automatically once a source is enabled, it is guarded:

* **A per-tick cap** on how many documents are processed.
* **A daily budget** in USD, measured from the ``cost_usd`` already recorded on every
  extraction -- so the guard uses the same numbers as the telemetry, not an estimate.
* **Only documents with quotable text.** A metadata-only document (the source's terms
  forbid storing its body) cannot yield evidence, so extracting it would buy nothing.
* **Bounded retries.** A document whose extraction keeps erroring is retried a few
  times, then left alone rather than retried forever.
* **One transaction per document** in the worker, so a crash mid-tick cannot roll back
  documents already processed and pay for them twice.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from gri.config import get_settings
from gri.logging import get_logger
from gri.models import Extraction, RawDocument
from gri.processing.embed import EmbeddingProvider
from gri.processing.extract import ExtractionProvider
from gri.processing.pipeline import process_document

log = get_logger(__name__)

#: Attempts before a document whose extraction keeps failing is left alone. Each attempt
#: can write up to two error rows (the first pass and its escalation).
MAX_ATTEMPTS = 3
MAX_ERROR_ROWS = MAX_ATTEMPTS * 2


@dataclass
class ProcessingTick:
    """What one pass over the pending documents did."""

    considered: int = 0
    processed: int = 0
    skipped_locked: int = 0
    failed: int = 0
    stopped_for_budget: bool = False
    spent_today_usd: float = 0.0


def pending_document_ids(session: Session, limit: int) -> list[uuid.UUID]:
    """Documents with quotable text that have not reached a terminal extraction outcome.

    Terminal means any outcome other than ``error`` -- including ``abstained`` and
    ``citation_invalid``, which are answers, not failures. Oldest first, so a backlog
    drains in order.
    """
    if limit <= 0:
        return []

    terminal = exists().where(
        and_(Extraction.document_id == RawDocument.id, Extraction.outcome != "error")
    )
    errors = (
        select(func.count())
        .select_from(Extraction)
        .where(Extraction.document_id == RawDocument.id, Extraction.outcome == "error")
        .scalar_subquery()
    )
    return list(
        session.scalars(
            select(RawDocument.id)
            .where(RawDocument.clean_text.is_not(None))
            .where(RawDocument.is_tombstoned.is_(False))
            .where(~terminal)
            .where(errors < MAX_ERROR_ROWS)
            .order_by(RawDocument.retrieved_at, RawDocument.id)
            .limit(limit)
        )
    )


def spent_today_usd(session: Session, now: datetime | None = None) -> float:
    """Recorded extraction spend since midnight UTC."""
    now = now or datetime.now(UTC)
    start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    total = session.scalar(
        select(func.coalesce(func.sum(Extraction.cost_usd), 0)).where(
            Extraction.created_at >= start
        )
    )
    return float(total if isinstance(total, int | float | Decimal) else 0)


def extraction_configured() -> bool:
    """Whether a model credential is available, so the worker does not try and fail.

    Checks the project setting and the SDK's own environment variables. An ``ant auth
    login`` profile is not detected here; set ``GRI_ANTHROPIC_API_KEY`` to be explicit.
    """
    if get_settings().anthropic_api_key:
        return True
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def process_pending(
    session: Session,
    *,
    limit: int,
    daily_budget_usd: float,
    embedding_provider: EmbeddingProvider | None = None,
    extraction_provider: ExtractionProvider | None = None,
    commit_each: bool = False,
    now: datetime | None = None,
) -> ProcessingTick:
    """Process up to ``limit`` pending documents without exceeding the daily budget.

    ``commit_each`` commits after every document; the worker sets it so finished work
    survives a crash. Tests leave it off and rely on savepoints, so their transaction can
    still be rolled back.
    """
    tick = ProcessingTick()
    if limit <= 0 or daily_budget_usd <= 0:
        return tick

    ids = pending_document_ids(session, limit)
    tick.considered = len(ids)

    for document_id in ids:
        tick.spent_today_usd = spent_today_usd(session, now)
        if tick.spent_today_usd >= daily_budget_usd:
            tick.stopped_for_budget = True
            log.warning(
                "daily_extraction_budget_reached",
                spent_usd=round(tick.spent_today_usd, 4),
                budget_usd=daily_budget_usd,
                remaining_pending=len(ids) - tick.processed - tick.failed - tick.skipped_locked,
            )
            break

        try:
            with session.begin_nested():
                # Lock this one document; another worker already holding it is skipped
                # rather than waited on or processed twice.
                document = session.scalar(
                    select(RawDocument)
                    .where(RawDocument.id == document_id)
                    .with_for_update(skip_locked=True)
                )
                if document is None:
                    tick.skipped_locked += 1
                    continue
                process_document(
                    session,
                    document,
                    embedding_provider=embedding_provider,
                    extraction_provider=extraction_provider,
                )
            tick.processed += 1
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the tick
            tick.failed += 1
            log.error("document_processing_failed", document_id=str(document_id), error=str(exc))
            continue

        if commit_each:
            session.commit()

    tick.spent_today_usd = spent_today_usd(session, now)
    if tick.considered:
        log.info(
            "processing_tick",
            considered=tick.considered,
            processed=tick.processed,
            failed=tick.failed,
            skipped_locked=tick.skipped_locked,
            stopped_for_budget=tick.stopped_for_budget,
            spent_today_usd=round(tick.spent_today_usd, 4),
        )
    return tick
