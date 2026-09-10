"""Constraint behaviour against a live Postgres.

The point of these tests is that the non-goals hold at the *database* level. A guarantee
that only exists in application code is one refactor away from being gone, so each of
these asserts that Postgres itself refuses the write.

Run with:  pytest -m integration   (requires the Compose db service)
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gri.models import Entity, Event, EventEvidence, RawDocument, Source

pytestmark = pytest.mark.integration

HASH_A = "a" * 64
HASH_B = "b" * 64


def _source(session: Session, **overrides: object) -> Source:
    defaults: dict[str, object] = {
        "slug": f"src-{uuid.uuid4().hex[:8]}",
        "name": "Test Notices",
        "publisher": "Test Authority",
        "feed_url": "https://example.invalid/feed.xml",
        "retrieval_method": "rss",
    }
    defaults.update(overrides)
    source = Source(**defaults)  # type: ignore[arg-type]
    session.add(source)
    session.flush()
    return source


def _document(session: Session, source: Source, content_hash: str = HASH_A) -> RawDocument:
    document = RawDocument(
        source_id=source.id,
        url="https://example.invalid/notice/1",
        retrieved_at=datetime.now(UTC),
        retrieval_method="rss",
        raw_hash=HASH_A,
        content_hash=content_hash,
        clean_text="The port of Example is closed to all traffic until further notice.",
    )
    session.add(document)
    session.flush()
    return document


def _event(**overrides: object) -> Event:
    defaults: dict[str, object] = {
        "event_type": "port_disruption",
        "event_date": date(2026, 9, 1),
        "title": "Example port closed",
        "summary": "The port of Example is closed to all traffic.",
        "severity": "low",
        "confidence": 0.9,
        "requires_human_review": False,
    }
    defaults.update(overrides)
    return Event(**defaults)  # type: ignore[arg-type]


class TestSourceTermsGate:
    """NG-2: an unreviewed source cannot be switched on, even by direct SQL."""

    def test_source_cannot_be_enabled_without_a_terms_review(self, session: Session) -> None:
        with pytest.raises(IntegrityError):
            _source(session, enabled=True)

    def test_source_can_be_enabled_once_terms_are_reviewed(self, session: Session) -> None:
        source = _source(
            session,
            enabled=True,
            terms_reviewed_at=datetime.now(UTC),
            terms_reviewed_by="a human",
            terms_url="https://example.invalid/terms",
        )
        assert source.enabled is True

    def test_full_text_storage_cannot_be_turned_on_without_a_review(self, session: Session) -> None:
        with pytest.raises(IntegrityError):
            _source(session, store_full_text=True)

    def test_polling_faster_than_thirty_minutes_is_rejected(self, session: Session) -> None:
        with pytest.raises(IntegrityError):
            _source(session, poll_interval_seconds=60)

    def test_adapters_default_to_disabled(self, session: Session) -> None:
        assert _source(session).enabled is False


class TestDocumentIdempotency:
    """Phase 1's gate depends on this: re-ingesting the same item must be a no-op."""

    def test_same_content_hash_from_same_source_is_rejected(self, session: Session) -> None:
        source = _source(session)
        _document(session, source, HASH_A)
        with pytest.raises(IntegrityError):
            _document(session, source, HASH_A)

    def test_same_content_hash_from_a_different_source_is_allowed(self, session: Session) -> None:
        _document(session, _source(session), HASH_A)
        second = _document(session, _source(session), HASH_A)
        assert second.id is not None

    def test_hash_must_be_a_sha256_hex_digest(self, session: Session) -> None:
        source = _source(session)
        with pytest.raises(IntegrityError):
            session.add(
                RawDocument(
                    source_id=source.id,
                    url="https://example.invalid/x",
                    retrieved_at=datetime.now(UTC),
                    retrieval_method="rss",
                    raw_hash="not-a-hash",
                    content_hash=HASH_B,
                )
            )
            session.flush()


class TestNoIndividualTargeting:
    """NG-4 at the database level: person-level records are not representable."""

    def test_a_person_cannot_be_stored_as_an_entity(self, session: Session) -> None:
        with pytest.raises(IntegrityError):
            session.add(Entity(canonical_name="Some Individual", entity_type="person"))
            session.flush()

    @pytest.mark.parametrize("entity_type", ["individual", "crew_member", "official"])
    def test_person_adjacent_types_are_rejected(self, session: Session, entity_type: str) -> None:
        with pytest.raises(IntegrityError):
            session.add(Entity(canonical_name="X", entity_type=entity_type))
            session.flush()

    def test_organisational_actors_are_allowed(self, session: Session) -> None:
        entity = Entity(canonical_name="Example Port Authority", entity_type="port_authority")
        session.add(entity)
        session.flush()
        assert entity.id is not None


class TestReviewTriggers:
    """CONSTRAINTS.md Section 6 triggers 1 and 4, enforced by CHECK constraints."""

    @pytest.mark.parametrize("severity", ["high", "severe"])
    def test_high_severity_cannot_skip_human_review(self, session: Session, severity: str) -> None:
        with pytest.raises(IntegrityError):
            session.add(_event(severity=severity, requires_human_review=False))
            session.flush()

    @pytest.mark.parametrize("severity", ["high", "severe"])
    def test_high_severity_is_fine_when_flagged(self, session: Session, severity: str) -> None:
        event = _event(severity=severity, requires_human_review=True)
        session.add(event)
        session.flush()
        assert event.event_id is not None

    def test_low_confidence_cannot_skip_human_review(self, session: Session) -> None:
        with pytest.raises(IntegrityError):
            session.add(_event(confidence=0.4, requires_human_review=False))
            session.flush()

    def test_confidence_outside_the_unit_interval_is_rejected(self, session: Session) -> None:
        with pytest.raises(IntegrityError):
            session.add(_event(confidence=1.5, requires_human_review=True))
            session.flush()

    def test_event_type_outside_the_taxonomy_is_rejected(self, session: Session) -> None:
        """NG-6: the taxonomy is closed, so scope cannot drift by adding a type."""
        with pytest.raises(IntegrityError):
            session.add(_event(event_type="general_geopolitical_risk"))
            session.flush()


class TestEvidenceIntegrity:
    def test_evidence_marked_verified_must_say_how(self, session: Session) -> None:
        source = _source(session)
        document = _document(session, source)
        event = _event(requires_human_review=True)
        session.add(event)
        session.flush()

        with pytest.raises(IntegrityError):
            session.add(
                EventEvidence(
                    event_id=event.event_id,
                    document_id=document.id,
                    field_supported="summary",
                    quote="closed to all traffic",
                    verified=True,  # but no method and no timestamp
                )
            )
            session.flush()

    def test_verified_evidence_with_provenance_is_accepted(self, session: Session) -> None:
        source = _source(session)
        document = _document(session, source)
        event = _event(requires_human_review=True)
        session.add(event)
        session.flush()

        evidence = EventEvidence(
            event_id=event.event_id,
            document_id=document.id,
            field_supported="summary",
            quote="closed to all traffic",
            verified=True,
            verification_method="exact_normalised_substring",
            verified_at=datetime.now(UTC),
        )
        session.add(evidence)
        session.flush()
        assert evidence.id is not None

    def test_blank_quotes_are_rejected(self, session: Session) -> None:
        source = _source(session)
        document = _document(session, source)
        event = _event(requires_human_review=True)
        session.add(event)
        session.flush()

        with pytest.raises(IntegrityError):
            session.add(
                EventEvidence(
                    event_id=event.event_id,
                    document_id=document.id,
                    field_supported="summary",
                    quote="   ",
                )
            )
            session.flush()
