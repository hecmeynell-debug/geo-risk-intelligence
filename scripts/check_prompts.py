"""Fail if a registered prompt was edited without its version being bumped.

ADR-0001 D6 requires prompt version to be recorded on every extraction, so that an
evaluation number is attributable to a specific prompt. That guarantee is worthless if
the text behind a version can change: an edited ``v1`` makes every historical ``v1``
measurement a lie about a prompt that no longer exists.

This compares each registered prompt's current hash against the hash stored in the
database. A mismatch means the template changed while the version did not.

Exit codes: 0 clean, 1 a prompt changed without a version bump, 2 could not reach the
database.
"""

from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from gri.db import session_scope
from gri.models import Prompt
from gri.processing.prompts import REGISTRY


def main() -> int:
    try:
        with session_scope() as session:
            stored = {(p.name, p.version): p.template_hash for p in session.scalars(select(Prompt))}
    except SQLAlchemyError as exc:
        print(f"error: could not reach the database: {exc}", file=sys.stderr)
        return 2

    violations: list[str] = []

    for (name, version), template in sorted(REGISTRY.items()):
        recorded = stored.get((name, version))
        if recorded is None:
            # Not yet used against this database. Nothing to contradict.
            print(f"{name} {version}: not yet recorded (hash {template.hash[:12]})")
            continue
        if recorded != template.hash:
            violations.append(
                f"{name} {version}: template hash {template.hash[:12]} does not match the"
                f" recorded {recorded[:12]} -- the prompt was edited without a version bump"
            )
        else:
            print(f"{name} {version}: unchanged")

    if violations:
        for violation in violations:
            print(f"VIOLATION: {violation}", file=sys.stderr)
        print(
            "\nBump the version in src/gri/processing/prompts.py instead of editing a"
            " released template.",
            file=sys.stderr,
        )
        return 1

    print(f"prompt integrity: OK ({len(REGISTRY)} registered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
