"""Scheduled worker entry point.

Polls every enabled source whose interval has elapsed, then extracts events from the
new documents -- within a per-tick cap and a daily spend budget -- and sleeps.

No Redis, no Celery: at a 30-60 minute cadence over a handful of sources, an in-process
loop plus ``FOR UPDATE SKIP LOCKED`` is sufficient (ADR-0001 D4). Adding a broker before
there is a demonstrated need would violate NG-7.

With no source enabled -- the state Phase 1 ships in -- each tick finds nothing due and
logs that it did nothing. That is correct, not broken: sources stay off until a human
records a terms review.
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import UTC, datetime
from types import FrameType

from sqlalchemy.exc import SQLAlchemyError

from gri.config import get_settings
from gri.db import check_database, session_scope
from gri.ingestion.http import HttpFetcher
from gri.ingestion.runner import ingest_due_sources
from gri.logging import configure_logging, get_logger
from gri.processing.queue import extraction_configured, pending_document_ids, process_pending

log = get_logger(__name__)

_shutdown = False

#: How often the loop wakes to look for due sources. Shorter than any source's poll
#: interval, because it is only a check -- the per-source interval decides what runs.
TICK_SECONDS = 60


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _shutdown
    log.info("shutdown_requested", signal=signum)
    _shutdown = True


def run_once(fetcher: HttpFetcher) -> dict[str, int]:
    """One scheduler tick: ingest what is due, then turn new documents into events."""
    settings = get_settings()
    now = datetime.now(UTC)
    totals = {
        "sources_run": 0,
        "items_new": 0,
        "items_duplicate": 0,
        "items_failed": 0,
        "documents_processed": 0,
        "documents_failed": 0,
    }

    with session_scope() as session:
        runs = ingest_due_sources(session, fetcher, now)
        totals["sources_run"] = len(runs)
        for run in runs:
            totals["items_new"] += run.items_new
            totals["items_duplicate"] += run.items_duplicate
            totals["items_failed"] += run.items_failed

    # Processing runs in its own session, committing after each document, so a crash
    # partway through cannot roll back work already paid for.
    if settings.max_extractions_per_tick and settings.daily_extraction_budget_usd:
        if extraction_configured():
            with session_scope() as session:
                tick = process_pending(
                    session,
                    limit=settings.max_extractions_per_tick,
                    daily_budget_usd=settings.daily_extraction_budget_usd,
                    commit_each=True,
                )
            totals["documents_processed"] = tick.processed
            totals["documents_failed"] = tick.failed
        else:
            # Only worth saying when something is actually waiting; otherwise a keyless
            # checkout would log this every tick forever.
            with session_scope() as session:
                if pending_document_ids(session, 1):
                    log.warning(
                        "extraction_not_configured",
                        hint="documents are waiting; set GRI_ANTHROPIC_API_KEY in .env",
                    )

    if any(totals.values()):
        log.info("tick_complete", **totals)
    return totals


def main() -> int:
    configure_logging()
    settings = get_settings()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("worker_starting", environment=settings.environment, tick_seconds=TICK_SECONDS)

    with session_scope() as session:
        log.info("worker_database", **check_database(session))

    with HttpFetcher() as fetcher:
        while not _shutdown:
            try:
                run_once(fetcher)
            except SQLAlchemyError as exc:
                # A database blip should not take the worker down, but it must be visible.
                log.error("tick_failed", error=str(exc))

            # Sleep in slices so SIGTERM is honoured promptly rather than a tick later.
            for _ in range(TICK_SECONDS):
                if _shutdown:
                    break
                time.sleep(1)

    log.info("worker_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
