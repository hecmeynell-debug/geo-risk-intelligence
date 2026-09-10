"""Running one source end to end, and selecting which sources are due.

Every run creates an ``ingestion_runs`` row before any network call and closes it
afterwards, so a crashed run is visible as a `running` row that never finished rather
than leaving no trace at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from gri.ingestion.http import FetchFailed, HttpFetcher, SourceBlocked
from gri.ingestion.registry import source_by_slug
from gri.ingestion.store import record_fetch_state, store_items
from gri.logging import get_logger
from gri.models import IngestionRun, Source

log = get_logger(__name__)


def due_sources(session: Session, now: datetime, lock: bool = True) -> list[Source]:
    """Enabled sources whose poll interval has elapsed.

    ``FOR UPDATE SKIP LOCKED`` means a second worker picks different rows instead of
    duplicating work or blocking -- which is why this needs no external queue (ADR-0001
    D4). Sources never fetched are due immediately.
    """
    statement = select(Source).where(Source.enabled.is_(True)).order_by(Source.slug)
    if lock:
        statement = statement.with_for_update(skip_locked=True)

    candidates = list(session.scalars(statement))
    return [s for s in candidates if _is_due(s, now)]


def _is_due(source: Source, now: datetime) -> bool:
    if source.last_fetched_at is None:
        return True
    due_at = source.last_fetched_at + timedelta(seconds=source.poll_interval_seconds)
    return now >= due_at


def ingest_source(
    session: Session,
    source: Source,
    fetcher: HttpFetcher,
    now: datetime | None = None,
) -> IngestionRun:
    """Fetch, parse, and store one source. Always returns a closed run row.

    Never raises for an ordinary fetch failure: the failure is recorded on the run and
    the worker moves on to the next source.
    """
    now = now or datetime.now(UTC)

    run = IngestionRun(source_id=source.id, started_at=now, status="running")
    session.add(run)
    session.flush()

    config = source_by_slug(source.slug)
    adapter = config.build_adapter()

    try:
        fetch = fetcher.fetch(
            source.feed_url,
            etag=source.last_etag,
            last_modified=source.last_modified_header,
        )
    except SourceBlocked as exc:
        # NG-2: a block is an answer. Disable the adapter and require a human to look.
        log.error("source_blocked", source=source.slug, error=str(exc))
        source.enabled = False
        source.disabled_reason = f"blocked at {now.isoformat()}: {exc}"
        record_fetch_state(source, None, now, succeeded=False)
        return _close(session, run, "failed", str(exc))
    except FetchFailed as exc:
        log.warning("source_fetch_failed", source=source.slug, error=str(exc))
        record_fetch_state(source, None, now, succeeded=False)
        return _close(session, run, "failed", str(exc))

    run.http_requests = fetch.attempts

    if fetch.not_modified:
        # The cheapest possible outcome, and the common one at a 30-minute cadence.
        record_fetch_state(source, fetch, now, succeeded=True)
        return _close(session, run, "skipped", None)

    items = adapter.parse(fetch.body, base_url=source.feed_url)
    log.info("source_parsed", source=source.slug, items=len(items))

    outcome = store_items(session, source, items, fetch, run)

    run.items_seen = outcome.seen
    run.items_new = outcome.new
    run.items_duplicate = outcome.duplicate
    run.items_failed = outcome.failed

    record_fetch_state(source, fetch, now, succeeded=True)
    log.info(
        "source_ingested",
        source=source.slug,
        seen=outcome.seen,
        new=outcome.new,
        duplicate=outcome.duplicate,
        failed=outcome.failed,
    )
    return _close(session, run, "succeeded", None)


def _close(session: Session, run: IngestionRun, status: str, error: str | None) -> IngestionRun:
    run.status = status
    run.error_message = error
    run.finished_at = datetime.now(UTC)
    session.flush()
    return run


def ingest_due_sources(
    session: Session,
    fetcher: HttpFetcher,
    now: datetime | None = None,
) -> list[IngestionRun]:
    """One scheduler tick: run every source that is due."""
    now = now or datetime.now(UTC)
    runs: list[IngestionRun] = []

    for source in due_sources(session, now):
        try:
            runs.append(ingest_source(session, source, fetcher, now))
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the tick
            log.error("source_run_crashed", source=source.slug, error=str(exc))
            session.rollback()

    return runs
