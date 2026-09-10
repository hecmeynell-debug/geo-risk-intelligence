"""Scheduled worker entry point.

Phase 0 is a heartbeat that proves the container starts, reaches the database, and finds
the schema migrated. Phase 1 replaces the body of the loop with source polling.

No Redis, no Celery: at a 30-60 minute cadence over a handful of sources, an in-process
loop plus Postgres row locks is sufficient (ADR-0001 D4). Adding a broker before there is
a demonstrated need would violate NG-7.
"""

from __future__ import annotations

import signal
import sys
import time
from types import FrameType

from sqlalchemy.exc import SQLAlchemyError

from gri.config import get_settings
from gri.db import check_database, session_scope
from gri.logging import configure_logging, get_logger

log = get_logger(__name__)

_shutdown = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _shutdown
    log.info("shutdown_requested", signal=signum)
    _shutdown = True


def run_once() -> dict[str, object]:
    """One tick. Phase 0: verify the database is reachable and migrated."""
    with session_scope() as session:
        status = check_database(session)
    log.info("worker_tick", **status)
    return status


def main() -> int:
    configure_logging()
    settings = get_settings()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info(
        "worker_starting",
        environment=settings.environment,
        poll_interval_seconds=settings.poll_interval_seconds,
    )

    while not _shutdown:
        try:
            run_once()
        except SQLAlchemyError as exc:
            # Keep running: a database blip should not take the worker down, but it must
            # be visible in the logs.
            log.error("worker_tick_failed", error=str(exc))

        # Sleep in short slices so SIGTERM is honoured promptly rather than after a
        # full poll interval.
        for _ in range(settings.poll_interval_seconds):
            if _shutdown:
                break
            time.sleep(1)

    log.info("worker_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
