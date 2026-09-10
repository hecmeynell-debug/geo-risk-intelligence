"""Seed the source registry into the database.

Idempotent: re-running updates the descriptive fields of existing rows and never touches
the review or enablement state. That matters -- a re-seed must not silently re-enable a
source or wipe a recorded terms review.

Every row is created disabled, unverified, and unreviewed (NG-2).
"""

from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from gri.db import session_scope
from gri.ingestion.registry import CANDIDATE_SOURCES
from gri.models import Source

#: Fields safe to refresh on an existing row. Deliberately excludes enabled,
#: terms_reviewed_*, store_full_text, endpoint_verified and format_confirmed: those are
#: human decisions and the seeder has no business overwriting them.
REFRESHABLE = ("name", "publisher", "homepage_url", "feed_url", "retrieval_method", "notes")


def _notes(config: object) -> str:
    parts = []
    rationale = getattr(config, "niche_rationale", "")
    review = getattr(config, "review_notes", "")
    if rationale:
        parts.append(f"Niche: {rationale}")
    if review:
        parts.append(f"Review: {review}")
    return "\n\n".join(parts)


def main() -> int:
    created = 0
    updated = 0

    try:
        with session_scope() as session:
            for config in CANDIDATE_SOURCES:
                existing = session.scalar(select(Source).where(Source.slug == config.slug))

                if existing is None:
                    session.add(
                        Source(
                            slug=config.slug,
                            name=config.name,
                            publisher=config.publisher,
                            homepage_url=config.homepage_url,
                            feed_url=config.feed_url,
                            retrieval_method=config.retrieval_method,
                            poll_interval_seconds=config.poll_interval_seconds,
                            enabled=False,
                            endpoint_verified=False,
                            format_confirmed=False,
                            store_full_text=False,
                            notes=_notes(config),
                        )
                    )
                    created += 1
                    continue

                changed = False
                for attribute in REFRESHABLE:
                    value = _notes(config) if attribute == "notes" else getattr(config, attribute)
                    if getattr(existing, attribute) != value:
                        setattr(existing, attribute, value)
                        changed = True
                if changed:
                    updated += 1
    except SQLAlchemyError as exc:
        print(f"error: could not reach the database: {exc}", file=sys.stderr)
        return 2

    print(f"sources: {created} created, {updated} updated, {len(CANDIDATE_SOURCES)} in registry")
    print("all sources are disabled and unreviewed -- run review_source_terms.py to enable one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
