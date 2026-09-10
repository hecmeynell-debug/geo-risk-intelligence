"""Record a terms review for a source, and optionally enable it.

This is the NG-2 gate in operator form. It cannot be run accidentally: enabling a source
requires the reviewer to pass their name, the terms URL, a note on what the terms
actually say, and explicit confirmation that they checked the endpoint and the parse.

The script does not read anyone's terms for you and does not judge them. It records what
a human decided, so that the decision is auditable later.

Examples
--------
List what is registered and what state it is in::

    python scripts/review_source_terms.py --list

Record a review without enabling (the usual first step)::

    python scripts/review_source_terms.py --slug panama-canal-advisories \\
        --reviewer "A Human" --terms-url https://example/terms \\
        --note "Terms permit automated retrieval; no redistribution of full text."

Enable a source once everything has been checked::

    python scripts/review_source_terms.py --slug panama-canal-advisories \\
        --reviewer "A Human" --terms-url https://example/terms \\
        --note "Terms permit automated retrieval and storage." \\
        --licence "Crown copyright, permitted" \\
        --endpoint-verified --format-confirmed --store-full-text --enable
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from gri.db import session_scope
from gri.models import Source


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--list", action="store_true", help="list registered sources and exit")
    parser.add_argument("--slug", help="source to review")
    parser.add_argument("--reviewer", help="who read the terms")
    parser.add_argument("--terms-url", help="link to the terms page that was read")
    parser.add_argument("--note", help="what the terms say about automated access and storage")
    parser.add_argument("--licence", help="licence or rights status")
    parser.add_argument(
        "--endpoint-verified",
        action="store_true",
        help="you confirmed the feed URL is correct and reachable",
    )
    parser.add_argument(
        "--format-confirmed",
        action="store_true",
        help="you looked at a real response and the adapter config parses it",
    )
    parser.add_argument(
        "--store-full-text",
        action="store_true",
        help="the terms permit storing the body text (required for quotable evidence)",
    )
    parser.add_argument(
        "--robots-checked", action="store_true", help="you checked robots.txt for the fetched path"
    )
    parser.add_argument("--enable", action="store_true", help="turn the source on")
    parser.add_argument("--disable", action="store_true", help="turn the source off")
    return parser.parse_args(argv)


def _list_sources() -> int:
    with session_scope() as session:
        sources = list(session.scalars(select(Source).order_by(Source.slug)))

    if not sources:
        print("no sources registered -- run scripts/seed_sources.py first")
        return 0

    print(f"{'slug':<36} {'on':<4} {'terms':<7} {'endpt':<7} {'fmt':<6} {'text':<5}")
    print("-" * 72)
    for source in sources:
        print(
            f"{source.slug:<36} "
            f"{'yes' if source.enabled else 'no':<4} "
            f"{'yes' if source.terms_reviewed_at else 'NO':<7} "
            f"{'yes' if source.endpoint_verified else 'NO':<7} "
            f"{'yes' if source.format_confirmed else 'NO':<6} "
            f"{'yes' if source.store_full_text else 'no':<5}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        if args.list:
            return _list_sources()

        if not args.slug:
            print("error: --slug is required (or use --list)", file=sys.stderr)
            return 2

        with session_scope() as session:
            source = session.scalar(select(Source).where(Source.slug == args.slug))
            if source is None:
                print(f"error: no source with slug {args.slug!r}", file=sys.stderr)
                return 2

            if args.disable:
                source.enabled = False
                source.disabled_reason = f"disabled by {args.reviewer or 'operator'}"
                print(f"{source.slug}: disabled")
                return 0

            recording_review = bool(args.reviewer or args.terms_url or args.note)
            if recording_review:
                missing = [
                    flag
                    for flag, value in (
                        ("--reviewer", args.reviewer),
                        ("--terms-url", args.terms_url),
                        ("--note", args.note),
                    )
                    if not value
                ]
                if missing:
                    print(
                        f"error: recording a review needs all of: {', '.join(missing)}",
                        file=sys.stderr,
                    )
                    return 2

                now = datetime.now(UTC)
                source.terms_reviewed_at = now
                source.terms_reviewed_by = args.reviewer
                source.terms_url = args.terms_url
                source.terms_note = args.note
                if args.licence:
                    source.licence = args.licence
                if args.robots_checked:
                    source.robots_checked_at = now
                print(f"{source.slug}: terms review recorded by {args.reviewer}")

            if args.endpoint_verified:
                source.endpoint_verified = True
            if args.format_confirmed:
                source.format_confirmed = True
            if args.store_full_text:
                source.store_full_text = True

            if args.enable:
                blockers = []
                if source.terms_reviewed_at is None:
                    blockers.append("no terms review recorded")
                if not source.endpoint_verified:
                    blockers.append("endpoint not verified (--endpoint-verified)")
                if not source.format_confirmed:
                    blockers.append("format not confirmed (--format-confirmed)")
                if blockers:
                    # The database would refuse this too; failing here gives a readable
                    # reason instead of a constraint violation.
                    print(f"error: cannot enable {source.slug}:", file=sys.stderr)
                    for blocker in blockers:
                        print(f"  - {blocker}", file=sys.stderr)
                    return 1

                source.enabled = True
                source.disabled_reason = None
                print(f"{source.slug}: ENABLED -- it will be polled on the next worker tick")

    except SQLAlchemyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
