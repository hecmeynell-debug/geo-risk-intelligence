"""Fail if any enabled source lacks a recorded terms review.

The database carries a CHECK constraint to the same effect, so this script is a
belt-and-braces audit that also produces a readable report for CI and for the reviewer.
It additionally flags sources whose robots check has gone stale.

Exit codes: 0 clean, 1 violation found, 2 could not reach the database.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from gri.db import session_scope
from gri.models import Source

#: How long a robots.txt check stays fresh before we want another look.
ROBOTS_MAX_AGE = timedelta(days=30)


def main() -> int:
    try:
        with session_scope() as session:
            sources = list(session.scalars(select(Source).order_by(Source.slug)))
    except SQLAlchemyError as exc:
        print(f"error: could not reach the database: {exc}", file=sys.stderr)
        return 2

    if not sources:
        print("no sources registered yet (expected before Phase 1)")
        return 0

    violations: list[str] = []
    warnings: list[str] = []
    now = datetime.now(UTC)

    for source in sources:
        if source.enabled and source.terms_reviewed_at is None:
            violations.append(f"{source.slug}: enabled with no recorded terms review (NG-2)")
        if source.store_full_text and source.terms_reviewed_at is None:
            violations.append(f"{source.slug}: stores full text with no terms review (NG-2)")
        if source.enabled and not source.terms_url:
            violations.append(f"{source.slug}: enabled with no terms_url recorded")

        if source.enabled and source.robots_checked_at is None:
            warnings.append(f"{source.slug}: robots.txt has never been checked")
        elif source.enabled and source.robots_checked_at is not None:
            age = now - source.robots_checked_at
            if age > ROBOTS_MAX_AGE:
                warnings.append(f"{source.slug}: robots.txt check is {age.days} days old")

    enabled = sum(1 for s in sources if s.enabled)
    print(f"checked {len(sources)} source(s), {enabled} enabled")

    for warning in warnings:
        print(f"warning: {warning}")

    if violations:
        for violation in violations:
            print(f"VIOLATION: {violation}", file=sys.stderr)
        return 1

    print("source terms policy: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
