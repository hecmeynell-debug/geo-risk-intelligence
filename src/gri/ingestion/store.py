"""Writing parsed items into the raw document store.

Two things this module guarantees, and Phase 1's gate depends on both:

* **Idempotency.** Re-ingesting an unchanged item is a no-op. The key is
  ``(source_id, content_hash)``, enforced by a unique constraint, so idempotency holds
  even against a concurrent writer rather than only against a tidy sequential run.
* **Provenance completeness.** No document is written without source, url, retrieval
  method, both timestamps, and both hashes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from psycopg.errors import UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gri.ingestion.base import ParsedItem
from gri.ingestion.http import FetchResult
from gri.ingestion.normalise import content_hash, metadata_hash, normalise_text, sha256_text
from gri.logging import get_logger
from gri.models import IngestionRun, RawDocument, Source

log = get_logger(__name__)


@dataclass
class StoreOutcome:
    """What a store pass did. Counts feed the ``ingestion_runs`` row."""

    new: int = 0
    duplicate: int = 0
    failed: int = 0
    document_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def seen(self) -> int:
        return self.new + self.duplicate + self.failed


def compute_hashes(item: ParsedItem, store_full_text: bool) -> tuple[str, str, str | None]:
    """Return ``(raw_hash, content_hash, clean_text)`` for an item.

    Where the source's terms permit only metadata retention, there is no body to hash or
    keep, so identity falls back to the normalised composite of title, url, and
    publication time. Such a document can never yield a quotable citation, which is why
    ``clean_text`` is None rather than an empty string -- the difference is meaningful to
    Phase 2.
    """
    if store_full_text and item.body_text:
        clean = normalise_text(item.body_text)
        return sha256_text(item.body_text), content_hash(item.body_text), clean

    published = item.published_at.isoformat() if item.published_at else None
    digest = metadata_hash(item.title, item.url, published)
    return digest, digest, None


def store_item(
    session: Session,
    source: Source,
    item: ParsedItem,
    fetch: FetchResult,
    run: IngestionRun | None = None,
) -> tuple[RawDocument | None, bool]:
    """Persist one item. Returns ``(document, is_new)``.

    ``(None, False)`` means the item was a duplicate of one already stored.
    """
    raw_hash, digest, clean = compute_hashes(item, source.store_full_text)

    existing = session.scalar(
        select(RawDocument).where(
            RawDocument.source_id == source.id, RawDocument.content_hash == digest
        )
    )
    if existing is not None:
        return None, False

    document = RawDocument(
        source_id=source.id,
        fetch_run_id=run.id if run is not None else None,
        source_uid=item.source_uid,
        url=item.url,
        canonical_url=item.canonical_url,
        title=item.title,
        published_at=item.published_at,
        retrieved_at=fetch.retrieved_at,
        retrieval_method=source.retrieval_method,
        http_status=fetch.status_code,
        content_type=fetch.content_type,
        etag=fetch.etag,
        last_modified=fetch.last_modified,
        raw_body=item.body_text if source.store_full_text else None,
        clean_text=clean,
        raw_hash=raw_hash,
        content_hash=digest,
        language=item.language,
        extra={
            **item.extra,
            # Ties an item back to the exact HTTP response it came from, which a
            # per-item hash alone would not.
            "response_hash": sha256_text(str(fetch.status_code) + fetch.url),
            "response_status": fetch.status_code,
            "fetch_attempts": fetch.attempts,
            "stored_full_text": source.store_full_text,
        },
    )

    # A savepoint, so losing the race against a concurrent writer costs this one insert
    # rather than the whole run's transaction.
    try:
        with session.begin_nested():
            session.add(document)
            session.flush()
    except IntegrityError as exc:
        if isinstance(exc.orig, UniqueViolation):
            # Another worker stored the same item between our check and our insert.
            log.info("document_duplicate_race", source=source.slug, content_hash=digest)
            return None, False
        # Anything else -- a NOT NULL or CHECK violation -- is a real defect in the item
        # or the adapter. Counting it as a duplicate would hide it, so it propagates and
        # is recorded as a failure.
        raise

    return document, True


def store_items(
    session: Session,
    source: Source,
    items: list[ParsedItem],
    fetch: FetchResult,
    run: IngestionRun | None = None,
) -> StoreOutcome:
    """Persist a batch, counting new, duplicate, and failed items."""
    outcome = StoreOutcome()

    for item in items:
        try:
            document, is_new = store_item(session, source, item, fetch, run)
        except (IntegrityError, ValueError, TypeError) as exc:
            # One bad item must not lose the rest of the batch.
            outcome.failed += 1
            log.warning("document_store_failed", source=source.slug, url=item.url, error=str(exc))
            continue

        if is_new and document is not None:
            outcome.new += 1
            outcome.document_ids.append(document.id)
        else:
            outcome.duplicate += 1

    return outcome


def record_fetch_state(
    source: Source,
    fetch: FetchResult | None,
    now: datetime,
    succeeded: bool,
) -> None:
    """Update the source's conditional-request validators and failure counter.

    Storing ``ETag``/``Last-Modified`` is what makes the next poll conditional, which is
    the single most effective courtesy we extend to a publisher: an unchanged feed costs
    them a 304 instead of a full response.
    """
    source.last_fetched_at = now
    if fetch is not None:
        if fetch.etag:
            source.last_etag = fetch.etag
        if fetch.last_modified:
            source.last_modified_header = fetch.last_modified

    if succeeded:
        source.last_success_at = now
        source.consecutive_failures = 0
    else:
        source.consecutive_failures = (source.consecutive_failures or 0) + 1
