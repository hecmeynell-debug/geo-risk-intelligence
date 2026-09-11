"""Shared builders for tests that need real events in the database.

Events are created by running a document through the actual pipeline with a scripted
extraction provider, rather than by inserting ``events`` rows directly. That way every
record a Phase 3 test looks at has verified evidence exactly as production would give
it -- a hand-inserted event could have no evidence at all, and a test built on one would
prove nothing about what the dashboard shows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from gri.ingestion.normalise import content_hash, sha256_text
from gri.models import Event, RawDocument, Source
from gri.processing.embed import DeterministicFakeProvider
from gri.processing.extract import ScriptedExtractionProvider
from gri.processing.pipeline import process_document
from tests import factories

DAY = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)


def make_source(session: Session, slug: str | None = None) -> Source:
    source = Source(
        slug=slug or f"test-{uuid.uuid4().hex[:8]}",
        name="Example Maritime Authority Notices",
        publisher="Example Maritime Authority",
        feed_url="https://notices.example.invalid/feed.xml",
        retrieval_method="rss",
        store_full_text=True,
        terms_reviewed_at=datetime.now(UTC),
        terms_reviewed_by="test",
        terms_url="https://notices.example.invalid/terms",
    )
    session.add(source)
    session.flush()
    return source


def seed_event(
    session: Session,
    source: Source,
    *,
    published_at: datetime = DAY,
    url: str | None = None,
    text: str = factories.NOTICE_TEXT,
    result: object | None = None,
    **event_kwargs: object,
) -> Event:
    """Run one synthetic document through the pipeline and return the event it made."""
    marker = uuid.uuid4().hex
    # A unique reference per document, so the fake embedder gives each its own vector
    # and seeded records stay separate events rather than consolidating (ADR-0004).
    # Appended at the end, so every factory quote is still a verbatim substring.
    text = f"{text} Ref {marker}."
    document = RawDocument(
        source_id=source.id,
        url=url or f"https://notices.example.invalid/notice/{marker}",
        title="Example Port closed",
        published_at=published_at,
        retrieved_at=published_at,
        retrieval_method="rss",
        clean_text=text,
        raw_body=text,
        raw_hash=sha256_text(text),
        # Unique per call so repeated seeding never trips the dedup constraint.
        content_hash=content_hash(text + marker),
    )
    session.add(document)
    session.flush()

    outcome = process_document(
        session,
        document,
        embedding_provider=DeterministicFakeProvider(),
        extraction_provider=ScriptedExtractionProvider(
            [result if result is not None else factories.make_result(**event_kwargs)]  # type: ignore[list-item]
        ),
    )
    assert outcome.event_id is not None, outcome.reasons
    event = session.get(Event, outcome.event_id)
    assert event is not None
    return event
