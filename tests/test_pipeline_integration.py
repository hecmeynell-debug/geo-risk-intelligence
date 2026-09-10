"""End-to-end pipeline against a live Postgres.

This module carries the Phase 2 gate: **source document -> validated, cited event record
works and is tested.**

The gate the project actually turns on is the negative one — `TestCitationGate` — which
proves a fabricated quote cannot reach the ``events`` table however confident the model
was. Extraction runs against a scripted provider, so the suite needs no key, no network,
and no spend.

Run with:  pytest -m integration
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gri.models import (
    DocumentChunk,
    Entity,
    Event,
    EventEvidence,
    Extraction,
    RawDocument,
    Source,
)
from gri.processing.embed import DeterministicFakeProvider
from gri.processing.extract import DEFAULT_MODEL, ESCALATION_MODEL, ScriptedExtractionProvider
from gri.processing.pipeline import chunk_and_embed, process_document
from tests import factories

pytestmark = pytest.mark.integration


@pytest.fixture
def embedder() -> DeterministicFakeProvider:
    return DeterministicFakeProvider()


@pytest.fixture
def source(session: Session) -> Source:
    src = Source(
        slug="pipeline-test-source",
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


def make_document(
    session: Session, source: Source, text: str = factories.NOTICE_TEXT
) -> RawDocument:
    from gri.ingestion.normalise import content_hash, sha256_text

    document = RawDocument(
        source_id=source.id,
        url="https://notices.example.invalid/notice/1001",
        title="Example Port closed",
        published_at=datetime(2026, 9, 1, 8, 30, tzinfo=UTC),
        retrieved_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        retrieval_method="rss",
        clean_text=text,
        raw_body=text,
        raw_hash=sha256_text(text),
        content_hash=content_hash(text),
    )
    session.add(document)
    session.flush()
    return document


class TestChunkingAndEmbedding:
    def test_chunks_are_stored_with_embeddings(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        created = chunk_and_embed(session, document, embedder)

        chunks = list(
            session.scalars(select(DocumentChunk).where(DocumentChunk.document_id == document.id))
        )
        assert created == len(chunks) > 0
        for chunk in chunks:
            assert chunk.embedding is not None
            assert len(chunk.embedding) == embedder.dimension
            assert chunk.embedding_model == embedder.model_name

    def test_chunk_offsets_locate_the_text_in_the_document(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        chunk_and_embed(session, document, embedder)

        for chunk in session.scalars(
            select(DocumentChunk).where(DocumentChunk.document_id == document.id)
        ):
            assert document.clean_text is not None
            assert document.clean_text[chunk.char_start : chunk.char_end] == chunk.text

    def test_chunking_is_idempotent(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        first = chunk_and_embed(session, document, embedder)
        second = chunk_and_embed(session, document, embedder)

        assert first > 0
        assert second == 0

    def test_metadata_only_document_yields_no_chunks(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        document.clean_text = None
        session.flush()
        assert chunk_and_embed(session, document, embedder) == 0


class TestHappyPath:
    """The Phase 2 gate: document in, cited event record out."""

    def test_produces_an_event_with_verified_evidence(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        provider = ScriptedExtractionProvider([factories.make_result(confidence=0.9)])

        result = process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        assert result.published
        assert result.outcome == "published"

        event = session.get(Event, result.event_id)
        assert event is not None
        assert event.event_type == "port_disruption"
        assert event.requires_human_review is False

        evidence = list(
            session.scalars(select(EventEvidence).where(EventEvidence.event_id == event.event_id))
        )
        assert evidence
        assert all(e.verified for e in evidence)
        assert all(e.verification_method and e.verified_at for e in evidence)

    def test_every_quote_is_locatable_in_the_stored_source_text(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        """The product promise, asserted against the database rather than in memory."""
        from gri.ingestion.normalise import normalise_text

        document = make_document(session, source)
        provider = ScriptedExtractionProvider([factories.make_result()])
        result = process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        haystack = normalise_text(document.clean_text or "")
        for evidence in session.scalars(
            select(EventEvidence).where(EventEvidence.event_id == result.event_id)
        ):
            assert normalise_text(evidence.quote) in haystack
            assert evidence.quote_char_start is not None
            assert haystack[evidence.quote_char_start : evidence.quote_char_end] == normalise_text(
                evidence.quote
            )

    def test_locations_actors_and_sectors_are_stored(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        provider = ScriptedExtractionProvider([factories.make_result()])
        result = process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        event = session.get(Event, result.event_id)
        assert event is not None
        assert [loc.name for loc in event.locations] == ["Example Port"]
        assert {s.sector for s in event.sectors} == {"ports_terminals", "maritime_shipping"}
        assert [a.entity.canonical_name for a in event.actors] == ["Example Port Authority"]
        assert all(a.entity.entity_type != "person" for a in event.actors)

    def test_event_links_back_to_its_source_document(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        provider = ScriptedExtractionProvider([factories.make_result()])
        result = process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        event = session.get(Event, result.event_id)
        assert event is not None
        assert [d.document_id for d in event.documents] == [document.id]


class TestCitationGate:
    """The check the whole project turns on."""

    def test_a_fabricated_quote_prevents_publication(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        provider = ScriptedExtractionProvider(
            [
                factories.make_result_with_fabricated_quote(),
                # The escalation pass repeats the same mistake.
                factories.make_result_with_fabricated_quote(),
            ]
        )

        result = process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        assert not result.published
        assert result.outcome == "citation_failed"
        assert session.scalar(select(func.count()).select_from(Event)) == 0

    def test_a_failed_extraction_still_records_its_telemetry(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        """A rejected record must still be visible in the evaluation data."""
        document = make_document(session, source)
        provider = ScriptedExtractionProvider([factories.make_result_with_fabricated_quote()] * 2)
        process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        extractions = list(session.scalars(select(Extraction)))
        assert extractions
        final = extractions[-1]
        assert final.citation_valid is False
        assert final.citations_failed >= 1
        assert final.event_id is None


class TestAbstentionAndScope:
    def test_abstention_is_recorded_as_a_success_not_an_error(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        provider = ScriptedExtractionProvider(
            [factories.make_abstention(), factories.make_abstention()]
        )

        result = process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        assert result.outcome == "abstained"
        assert not result.published
        assert session.scalar(select(func.count()).select_from(Event)) == 0
        assert {e.outcome for e in session.scalars(select(Extraction))} == {"abstained"}

    def test_out_of_niche_document_creates_no_event(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        provider = ScriptedExtractionProvider([factories.make_out_of_niche()])

        result = process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        assert result.outcome == "background"
        assert session.scalar(select(func.count()).select_from(Event)) == 0
        # Out of niche is cheap and reliable, so it is not escalated.
        assert len(provider.calls_made) == 1


class TestEscalationCascade:
    def test_weak_first_pass_escalates_to_the_stronger_model(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        provider = ScriptedExtractionProvider(
            [factories.make_result(confidence=0.3), factories.make_result(confidence=0.9)]
        )

        result = process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        assert [model for model, _ in provider.calls_made] == [DEFAULT_MODEL, ESCALATION_MODEL]
        assert result.extraction is not None
        assert result.extraction.did_escalate
        assert result.published

    def test_strong_first_pass_does_not_escalate(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        provider = ScriptedExtractionProvider([factories.make_result(confidence=0.95)])

        result = process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        assert [model for model, _ in provider.calls_made] == [DEFAULT_MODEL]
        assert result.extraction is not None
        assert not result.extraction.did_escalate

    def test_both_halves_of_a_cascade_are_recorded_separately(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        """Cost and quality must be attributable to the model that produced them."""
        document = make_document(session, source)
        provider = ScriptedExtractionProvider(
            [factories.make_result(confidence=0.3), factories.make_result(confidence=0.9)]
        )
        process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        extractions = list(session.scalars(select(Extraction).order_by(Extraction.created_at)))
        assert len(extractions) == 2
        assert {e.model_name for e in extractions} == {DEFAULT_MODEL, ESCALATION_MODEL}
        for row in extractions:
            assert row.prompt_version == "v2"
            assert row.input_tokens and row.output_tokens
            assert row.cost_usd is not None and float(row.cost_usd) > 0
            # Cache accounting is persisted even when zero, so a cache that stops
            # hitting shows up in the data rather than only in the bill.
            assert row.cache_write_tokens is not None
            assert row.cache_read_tokens is not None

    def test_escalation_prompt_explains_why(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        provider = ScriptedExtractionProvider(
            [factories.make_result(confidence=0.3), factories.make_result(confidence=0.9)]
        )
        process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        _, escalation_prompt = provider.calls_made[1]
        assert "Second pass" in escalation_prompt
        assert "below" in escalation_prompt


class TestReviewRouting:
    @pytest.mark.parametrize("severity", ["high", "severe"])
    def test_high_severity_is_routed_to_review(
        self,
        session: Session,
        source: Source,
        embedder: DeterministicFakeProvider,
        severity: str,
    ) -> None:
        document = make_document(session, source)
        provider = ScriptedExtractionProvider(
            [factories.make_result(severity=severity, confidence=0.95)]
        )

        result = process_document(
            session, document, embedding_provider=embedder, extraction_provider=provider
        )

        assert result.outcome == "needs_review"
        event = session.get(Event, result.event_id)
        assert event is not None
        assert event.requires_human_review is True
        assert event.review_status == "pending"


class TestClustering:
    def test_a_follow_up_notice_joins_the_same_event_cluster(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        first = make_document(session, source, factories.NOTICE_TEXT)
        r1 = process_document(
            session,
            first,
            embedding_provider=embedder,
            extraction_provider=ScriptedExtractionProvider([factories.make_result()]),
        )

        second = make_document(session, source, factories.SECOND_NOTICE_TEXT)
        second.url = "https://notices.example.invalid/notice/1002"
        session.flush()

        # Force a match by reusing the first document's centroid: the deterministic fake
        # embedder is a fixture, not a semantic model, so similarity is asserted here
        # through the clustering code path rather than through pretend meaning.
        from gri.processing.cluster import assign_to_cluster, document_centroid

        vector = document_centroid(session, first.id)
        cluster, match = assign_to_cluster(session, vector, second.published_at)

        assert not match.is_new
        assert cluster.member_count == 2
        assert session.get(Event, r1.event_id) is not None

    def test_an_unrelated_notice_starts_a_new_cluster(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        first = make_document(session, source, factories.NOTICE_TEXT)
        process_document(
            session,
            first,
            embedding_provider=embedder,
            extraction_provider=ScriptedExtractionProvider([factories.make_result()]),
        )

        unrelated = make_document(session, source, factories.UNRELATED_NOTICE_TEXT)
        unrelated.url = "https://notices.example.invalid/notice/2001"
        session.flush()

        # Quotes must come from THIS document: verification is per-document, so reusing
        # the maritime factory here would (correctly) fail the citation gate.
        result = process_document(
            session,
            unrelated,
            embedding_provider=embedder,
            extraction_provider=ScriptedExtractionProvider([factories.make_grid_result()]),
        )

        assert result.published, result.reasons
        second_event = session.get(Event, result.event_id)
        first_event = session.scalar(select(Event).where(Event.event_id != result.event_id))

        assert first_event is not None and second_event is not None
        assert first_event.cluster_id is not None
        assert first_event.cluster_id != second_event.cluster_id


class TestNoIndividualTargeting:
    def test_entities_created_by_the_pipeline_are_never_people(
        self, session: Session, source: Source, embedder: DeterministicFakeProvider
    ) -> None:
        document = make_document(session, source)
        process_document(
            session,
            document,
            embedding_provider=embedder,
            extraction_provider=ScriptedExtractionProvider([factories.make_result()]),
        )

        for entity in session.scalars(select(Entity)):
            assert entity.entity_type != "person"
