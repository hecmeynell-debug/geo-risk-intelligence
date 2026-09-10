"""End-to-end ingestion against a live Postgres.

This module carries the Phase 1 gate: **repeated ingestion is idempotent at document
level and fully provenance-tracked.** The idempotency tests run the real runner twice
over the same fixture feed and assert the second pass writes nothing new.

Run with:  pytest -m integration
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gri.ingestion.http import HttpFetcher
from gri.ingestion.registry import CANDIDATE_SOURCES, SourceConfig
from gri.ingestion.runner import due_sources, ingest_source
from gri.ingestion.store import store_items
from gri.models import IngestionRun, RawDocument, Source
from tests import fixtures

pytestmark = pytest.mark.integration

FEED_URL = "https://notices.example.invalid/feed.xml"


@pytest.fixture
def rss_config(monkeypatch: pytest.MonkeyPatch) -> SourceConfig:
    """Register a synthetic RSS source so the runner can look it up by slug.

    The runner resolves adapters through the registry, so a test source has to be in the
    registry. Patching the module-level tuple keeps that honest rather than adding a
    test-only backdoor to production code.
    """
    config = SourceConfig(
        slug="test-rss-source",
        name="Test RSS Notices",
        publisher="Example Maritime Authority",
        feed_url=FEED_URL,
        retrieval_method="rss",
        niche_rationale="test",
        review_notes="test",
    )
    monkeypatch.setattr("gri.ingestion.registry.CANDIDATE_SOURCES", (*CANDIDATE_SOURCES, config))
    return config


@pytest.fixture
def source(session: Session, rss_config: SourceConfig) -> Source:
    """A reviewed, verified, enabled source -- the state a human would have to create."""
    now = datetime.now(UTC)
    source = Source(
        slug=rss_config.slug,
        name=rss_config.name,
        publisher=rss_config.publisher,
        feed_url=rss_config.feed_url,
        retrieval_method="rss",
        enabled=True,
        endpoint_verified=True,
        format_confirmed=True,
        store_full_text=True,
        terms_reviewed_at=now,
        terms_reviewed_by="test",
        terms_url="https://notices.example.invalid/terms",
    )
    session.add(source)
    session.flush()
    return source


def fetcher_serving(*payloads: bytes) -> HttpFetcher:
    """A fetcher that returns each payload in turn, repeating the last one."""
    sequence = list(payloads)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return httpx.Response(
            200, content=sequence[index], headers={"content-type": "application/rss+xml"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpFetcher(client=client, sleep=lambda _s: None, enforce_robots=False)


def _documents(session: Session, source: Source) -> list[RawDocument]:
    return list(
        session.scalars(
            select(RawDocument)
            .where(RawDocument.source_id == source.id)
            .order_by(RawDocument.source_uid)
        )
    )


class TestIdempotency:
    """The Phase 1 gate."""

    def test_second_identical_run_writes_nothing_new(
        self, session: Session, source: Source
    ) -> None:
        fetcher = fetcher_serving(fixtures.RSS_FEED)

        first = ingest_source(session, source, fetcher)
        second = ingest_source(session, source, fetcher)

        assert (first.items_new, first.items_duplicate) == (2, 0)
        assert (second.items_new, second.items_duplicate) == (0, 2)
        assert len(_documents(session, source)) == 2

    def test_running_five_times_still_stores_two_documents(
        self, session: Session, source: Source
    ) -> None:
        fetcher = fetcher_serving(fixtures.RSS_FEED)
        for _ in range(5):
            ingest_source(session, source, fetcher)

        assert (
            session.scalar(
                select(func.count())
                .select_from(RawDocument)
                .where(RawDocument.source_id == source.id)
            )
            == 2
        )

    def test_reformatted_feed_is_not_new_documents(self, session: Session, source: Source) -> None:
        """A publisher re-rendering its feed must not fork every document."""
        fetcher = fetcher_serving(fixtures.RSS_FEED, fixtures.RSS_FEED_REFORMATTED)

        ingest_source(session, source, fetcher)
        second = ingest_source(session, source, fetcher)

        assert second.items_new == 0
        assert second.items_duplicate == 2
        assert len(_documents(session, source)) == 2

    def test_a_genuinely_new_item_is_stored(self, session: Session, source: Source) -> None:
        fetcher = fetcher_serving(fixtures.RSS_FEED, fixtures.RSS_FEED_WITH_NEW_ITEM)

        ingest_source(session, source, fetcher)
        second = ingest_source(session, source, fetcher)

        assert second.items_new == 1
        assert second.items_duplicate == 2
        assert len(_documents(session, source)) == 3

    def test_edited_text_creates_a_new_document(self, session: Session, source: Source) -> None:
        """An edit is a new document -- Phase 2 decides whether it updates an event."""
        edited = fixtures.RSS_FEED.replace(b"up to 72 hours", b"up to 96 hours")
        fetcher = fetcher_serving(fixtures.RSS_FEED, edited)

        ingest_source(session, source, fetcher)
        second = ingest_source(session, source, fetcher)

        assert second.items_new == 1
        assert len(_documents(session, source)) == 3


class TestProvenance:
    def test_every_document_carries_full_provenance(self, session: Session, source: Source) -> None:
        ingest_source(session, source, fetcher_serving(fixtures.RSS_FEED))

        for document in _documents(session, source):
            assert document.source_id == source.id
            assert document.url.startswith("https://")
            assert document.retrieval_method == "rss"
            assert document.retrieved_at is not None
            assert document.published_at is not None
            assert len(document.raw_hash) == 64
            assert len(document.content_hash) == 64
            assert document.fetch_run_id is not None
            assert document.http_status == 200

    def test_published_and_retrieved_times_are_distinct(
        self, session: Session, source: Source
    ) -> None:
        """Reporting lag is a real signal in this domain; conflating these destroys it."""
        ingest_source(session, source, fetcher_serving(fixtures.RSS_FEED))

        document = _documents(session, source)[0]
        assert document.published_at == datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
        assert document.retrieved_at > document.published_at

    def test_documents_link_back_to_their_ingestion_run(
        self, session: Session, source: Source
    ) -> None:
        run = ingest_source(session, source, fetcher_serving(fixtures.RSS_FEED))

        for document in _documents(session, source):
            assert document.fetch_run_id == run.id

    def test_clean_text_is_stored_for_citation_verification(
        self, session: Session, source: Source
    ) -> None:
        ingest_source(session, source, fetcher_serving(fixtures.RSS_FEED))

        document = _documents(session, source)[0]
        assert document.clean_text
        assert "closed to all traffic" in document.clean_text


class TestTermsRespectedAtIngestion:
    def test_metadata_only_source_stores_no_body(self, session: Session, source: Source) -> None:
        """NG-2: a source whose terms forbid storing the body must not store one."""
        source.store_full_text = False
        session.flush()

        ingest_source(session, source, fetcher_serving(fixtures.RSS_FEED))

        documents = _documents(session, source)
        assert documents
        for document in documents:
            assert document.raw_body is None
            assert document.clean_text is None
            # Still deduplicable, and still linked to its source.
            assert len(document.content_hash) == 64
            assert document.url.startswith("https://")

    def test_metadata_only_source_is_still_idempotent(
        self, session: Session, source: Source
    ) -> None:
        source.store_full_text = False
        session.flush()
        fetcher = fetcher_serving(fixtures.RSS_FEED)

        ingest_source(session, source, fetcher)
        second = ingest_source(session, source, fetcher)

        assert second.items_new == 0
        assert second.items_duplicate == 2


class TestRunBookkeeping:
    def test_successful_run_is_recorded(self, session: Session, source: Source) -> None:
        run = ingest_source(session, source, fetcher_serving(fixtures.RSS_FEED))

        assert run.status == "succeeded"
        assert run.finished_at is not None
        assert run.items_seen == 2
        assert run.error_message is None

    def test_not_modified_is_recorded_as_skipped(self, session: Session, source: Source) -> None:
        source.last_etag = '"abc123"'
        session.flush()

        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(304)))
        fetcher = HttpFetcher(client=client, sleep=lambda _s: None, enforce_robots=False)

        run = ingest_source(session, source, fetcher)

        assert run.status == "skipped"
        assert run.items_seen == 0
        assert source.last_success_at is not None

    def test_fetch_failure_is_recorded_without_raising(
        self, session: Session, source: Source
    ) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
        fetcher = HttpFetcher(client=client, sleep=lambda _s: None, enforce_robots=False)

        run = ingest_source(session, source, fetcher)

        assert run.status == "failed"
        assert run.error_message
        assert source.consecutive_failures == 1
        # A failure must not disable a source; only a block does that.
        assert source.enabled is True

    def test_a_block_disables_the_source(self, session: Session, source: Source) -> None:
        """NG-2: a block is an answer. Stop fetching and make a human look."""
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
        fetcher = HttpFetcher(client=client, sleep=lambda _s: None, enforce_robots=False)

        run = ingest_source(session, source, fetcher)

        assert run.status == "failed"
        assert source.enabled is False
        assert source.disabled_reason and "403" in source.disabled_reason

    def test_etag_is_captured_for_the_next_poll(self, session: Session, source: Source) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=fixtures.RSS_FEED, headers={"etag": '"v1"'})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        fetcher = HttpFetcher(client=client, sleep=lambda _s: None, enforce_robots=False)

        ingest_source(session, source, fetcher)
        assert source.last_etag == '"v1"'


class TestScheduling:
    def test_disabled_sources_are_never_due(self, session: Session, source: Source) -> None:
        source.enabled = False
        session.flush()
        assert source not in due_sources(session, datetime.now(UTC), lock=False)

    def test_a_never_fetched_source_is_due(self, session: Session, source: Source) -> None:
        assert source in due_sources(session, datetime.now(UTC), lock=False)

    def test_a_recently_fetched_source_is_not_due(self, session: Session, source: Source) -> None:
        now = datetime.now(UTC)
        source.last_fetched_at = now - timedelta(seconds=60)
        session.flush()
        assert source not in due_sources(session, now, lock=False)

    def test_a_source_past_its_interval_is_due(self, session: Session, source: Source) -> None:
        now = datetime.now(UTC)
        source.last_fetched_at = now - timedelta(seconds=source.poll_interval_seconds + 1)
        session.flush()
        assert source in due_sources(session, now, lock=False)


class TestStoreDirectly:
    def test_failed_items_are_counted_not_raised(self, session: Session, source: Source) -> None:
        """One bad item must not lose the rest of the batch."""
        from gri.ingestion.base import ParsedItem
        from gri.ingestion.http import FetchResult

        fetch = FetchResult(
            url=FEED_URL,
            status_code=200,
            body=b"",
            content_type="application/rss+xml",
            etag=None,
            last_modified=None,
            retrieved_at=datetime.now(UTC),
        )
        items = [
            ParsedItem(source_uid="ok-1", url="https://notices.example.invalid/1", body_text="a"),
            # url is NOT NULL in the schema, so this row cannot be written.
            ParsedItem(source_uid="bad", url=None, body_text="b"),  # type: ignore[arg-type]
            ParsedItem(source_uid="ok-2", url="https://notices.example.invalid/2", body_text="c"),
        ]

        outcome = store_items(session, source, items, fetch)

        assert outcome.new == 2
        assert outcome.failed == 1


def test_runs_are_traceable_to_a_source(session: Session, source: Source) -> None:
    ingest_source(session, source, fetcher_serving(fixtures.RSS_FEED))

    runs = list(session.scalars(select(IngestionRun).where(IngestionRun.source_id == source.id)))
    assert len(runs) == 1
    assert isinstance(runs[0].id, uuid.UUID)
