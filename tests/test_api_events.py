"""Event and search endpoints against a live database.

The behaviour these pin is not the HTTP plumbing but the product promise: an event is
never served without the quotes supporting it, and a record flagged for human review is
never served as though it were established.

Run with:  pytest -m integration
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from gri.api.main import create_app
from gri.db import get_db
from gri.models import RawDocument, Source
from gri.processing.embed import DeterministicFakeProvider
from gri.processing.extract import ScriptedExtractionProvider
from gri.processing.pipeline import process_document
from tests import factories

pytestmark = pytest.mark.integration


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    """A client bound to the test session, so it sees uncommitted fixture rows."""
    app = create_app()

    def override() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def source(session: Session) -> Source:
    src = Source(
        slug="api-test-source",
        name="Example Maritime Authority Notices",
        publisher="Example Maritime Authority",
        feed_url="https://notices.example.invalid/feed.xml",
        retrieval_method="rss",
        store_full_text=True,
        terms_reviewed_at=datetime.now(UTC),
        terms_reviewed_by="test",
        terms_url="https://notices.example.invalid/terms",
    )
    session.add(src)
    session.flush()
    return src


def seed_event(session: Session, source: Source, **kwargs: object) -> str:
    """Run a document through the pipeline and return the created event id."""
    from gri.ingestion.normalise import content_hash, sha256_text

    text = factories.NOTICE_TEXT
    document = RawDocument(
        source_id=source.id,
        url=f"https://notices.example.invalid/notice/{abs(hash(str(kwargs))) % 10000}",
        title="Example Port closed",
        published_at=datetime(2026, 9, 1, 8, 30, tzinfo=UTC),
        retrieved_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        retrieval_method="rss",
        clean_text=text,
        raw_body=text,
        raw_hash=sha256_text(text),
        content_hash=content_hash(text + str(kwargs)),
    )
    session.add(document)
    session.flush()

    result = process_document(
        session,
        document,
        embedding_provider=DeterministicFakeProvider(),
        extraction_provider=ScriptedExtractionProvider([factories.make_result(**kwargs)]),  # type: ignore[arg-type]
    )
    assert result.event_id is not None, result.reasons
    return str(result.event_id)


class TestEventDetailCarriesItsEvidence:
    def test_detail_includes_verified_quotes_and_source_links(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event_id = seed_event(session, source)

        body = client.get(f"/events/{event_id}").json()

        assert body["evidence"], "an event served without its evidence is unauditable"
        for quote in body["evidence"]:
            assert quote["verified"] is True
            assert quote["verification_method"] == "exact_normalised_substring"
            assert quote["source_url"].startswith("https://")
            assert quote["quote"]
        assert body["source_links"]

    def test_quotes_are_locatable_in_the_source(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        from gri.ingestion.normalise import normalise_text

        event_id = seed_event(session, source)
        body = client.get(f"/events/{event_id}").json()

        haystack = normalise_text(factories.NOTICE_TEXT)
        for quote in body["evidence"]:
            assert normalise_text(quote["quote"]) in haystack

    def test_detail_restates_the_limitations(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        """NG-1: a consumer must not be able to strip the context by reading one record."""
        event_id = seed_event(session, source)
        disclaimer = client.get(f"/events/{event_id}").json()["disclaimer"].lower()

        assert "not comprehensive" in disclaimer
        assert "verified" in disclaimer

    def test_unknown_event_is_404(self, client: TestClient) -> None:
        response = client.get("/events/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404


class TestReviewRecordsAreNotServedAsEstablished:
    def test_flagged_records_are_excluded_by_default(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="severe", confidence=0.95)

        body = client.get("/events").json()
        assert body["total"] == 0

    def test_flagged_records_must_be_asked_for_explicitly(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="severe", confidence=0.95)

        body = client.get("/events", params={"include_pending_review": True}).json()
        assert body["total"] == 1
        assert body["items"][0]["requires_human_review"] is True

    def test_established_records_are_listed(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="low", confidence=0.95)

        body = client.get("/events").json()
        assert body["total"] == 1
        assert body["items"][0]["requires_human_review"] is False


class TestFilteringAndPaging:
    def test_filter_by_event_type(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="low")

        assert client.get("/events", params={"event_type": "port_disruption"}).json()["total"] == 1
        assert client.get("/events", params={"event_type": "refinery_outage"}).json()["total"] == 0

    def test_filter_by_severity(self, client: TestClient, session: Session, source: Source) -> None:
        seed_event(session, source, severity="low")
        assert client.get("/events", params={"severity": "low"}).json()["total"] == 1
        assert client.get("/events", params={"severity": "moderate"}).json()["total"] == 0

    def test_filter_by_date_range(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="low")
        assert client.get("/events", params={"date_from": "2026-08-01"}).json()["total"] == 1
        assert client.get("/events", params={"date_from": "2026-10-01"}).json()["total"] == 0

    def test_filter_by_country(self, client: TestClient, session: Session, source: Source) -> None:
        seed_event(session, source, severity="low")
        assert client.get("/events", params={"country": "GB"}).json()["total"] == 1
        assert client.get("/events", params={"country": "FR"}).json()["total"] == 0

    def test_unknown_event_type_is_rejected(self, client: TestClient) -> None:
        response = client.get("/events", params={"event_type": "not_a_real_type"})
        assert response.status_code == 422

    def test_unknown_severity_is_rejected(self, client: TestClient) -> None:
        assert client.get("/events", params={"severity": "catastrophic"}).status_code == 422

    def test_paging_reports_total_independently_of_limit(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        for index in range(3):
            seed_event(session, source, severity="low", confidence=0.9 - index * 0.01)

        body = client.get("/events", params={"limit": 2}).json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["limit"] == 2


class TestSearch:
    def test_finds_an_event_by_title(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="low")
        body = client.get("/search", params={"q": "Example Port"}).json()

        assert body["total"] == 1
        assert body["items"][0]["matched_on"] in {"title", "summary", "evidence"}

    def test_finds_an_event_by_a_phrase_only_in_its_evidence(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        """The point of lexical search: you can see the words that matched."""
        seed_event(session, source, severity="low")
        body = client.get("/search", params={"q": "channel obstruction"}).json()
        assert body["total"] == 1

    def test_no_match_returns_empty(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="low")
        assert client.get("/search", params={"q": "refinery in Rotterdam"}).json()["total"] == 0

    def test_search_excludes_flagged_records_by_default(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="severe", confidence=0.95)
        assert client.get("/search", params={"q": "Example Port"}).json()["total"] == 0

    def test_search_is_case_insensitive(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="low")
        assert client.get("/search", params={"q": "example port"}).json()["total"] == 1

    def test_query_must_be_long_enough_to_be_useful(self, client: TestClient) -> None:
        assert client.get("/search", params={"q": "a"}).status_code == 422
