"""Document -> validated, cited event record.

This is the Phase 2 gate in one function. The ordering matters and is not arbitrary:

    chunk -> embed -> cluster -> extract -> validate schema -> VERIFY CITATIONS
          -> score confidence -> publish OR route to review

Citation verification sits *between* the model and the database. Nothing reaches the
``events`` table without passing it, and the ``extractions`` table carries a CHECK
constraint saying the same thing, so a bug here cannot publish an unverified record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from gri.logging import get_logger
from gri.models import (
    DocumentChunk,
    Entity,
    Event,
    EventActor,
    EventDocument,
    EventEvidence,
    EventLocation,
    EventSector,
    Extraction,
    Prompt,
    RawDocument,
    Source,
)
from gri.processing import chunk as chunking
from gri.processing.cluster import assign_to_cluster, document_centroid
from gri.processing.confidence import assess
from gri.processing.embed import EmbeddingProvider
from gri.processing.embed import get_provider as get_embedding_provider
from gri.processing.extract import (
    ExtractionOutcome,
    ExtractionProvider,
    extract_document,
)
from gri.processing.extract import (
    get_provider as get_extraction_provider,
)
from gri.processing.prompts import EXTRACT_EVENT_V1
from gri.processing.verify import VerificationReport, verify_evidence
from gri.schemas import ExtractionResult

log = get_logger(__name__)


@dataclass
class PipelineResult:
    """What processing one document produced."""

    document_id: uuid.UUID
    chunks_created: int = 0
    event_id: uuid.UUID | None = None
    outcome: str = "pending"
    verification: VerificationReport | None = None
    extraction: ExtractionOutcome | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def published(self) -> bool:
        return self.event_id is not None


def chunk_and_embed(
    session: Session,
    document: RawDocument,
    provider: EmbeddingProvider | None = None,
) -> int:
    """Chunk a document's ``clean_text`` and store embeddings. Idempotent."""
    provider = provider or get_embedding_provider()

    if not document.clean_text:
        # A metadata-only source (terms permit no body text) has nothing to chunk.
        return 0

    existing = session.scalar(
        select(DocumentChunk).where(DocumentChunk.document_id == document.id).limit(1)
    )
    if existing is not None:
        return 0

    chunks = chunking.chunk_text(document.clean_text)
    if not chunks:
        return 0

    if not chunking.verify_offsets(document.clean_text, chunks):
        # This would silently break citation location later, so it fails now and loudly.
        raise ValueError(f"chunk offsets do not match clean_text for document {document.id}")

    vectors = provider.embed([c.text for c in chunks])

    for piece, vector in zip(chunks, vectors, strict=True):
        session.add(
            DocumentChunk(
                document_id=document.id,
                chunk_index=piece.index,
                text=piece.text,
                char_start=piece.char_start,
                char_end=piece.char_end,
                embedding=vector,
                embedding_model=provider.model_name,
                embedding_dim=provider.dimension,
            )
        )
    session.flush()
    log.info("document_chunked", document_id=str(document.id), chunks=len(chunks))
    return len(chunks)


def _get_or_create_prompt(session: Session) -> Prompt:
    prompt = session.scalar(
        select(Prompt).where(
            Prompt.name == EXTRACT_EVENT_V1.name, Prompt.version == EXTRACT_EVENT_V1.version
        )
    )
    if prompt is not None:
        return prompt
    prompt = Prompt(
        name=EXTRACT_EVENT_V1.name,
        version=EXTRACT_EVENT_V1.version,
        purpose=EXTRACT_EVENT_V1.purpose,
        template=EXTRACT_EVENT_V1.template,
        template_hash=EXTRACT_EVENT_V1.hash,
    )
    session.add(prompt)
    session.flush()
    return prompt


def _get_or_create_entity(session: Session, name: str, entity_type: str) -> Entity:
    entity = session.scalar(
        select(Entity).where(Entity.canonical_name == name, Entity.entity_type == entity_type)
    )
    if entity is not None:
        return entity
    entity = Entity(canonical_name=name, entity_type=entity_type)
    session.add(entity)
    session.flush()
    return entity


def process_document(
    session: Session,
    document: RawDocument,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    extraction_provider: ExtractionProvider | None = None,
    allow_escalation: bool = True,
) -> PipelineResult:
    """Run one document through the whole pipeline."""
    result = PipelineResult(document_id=document.id)
    source = session.get(Source, document.source_id)
    if source is None:
        raise ValueError(f"document {document.id} has no source")

    result.chunks_created = chunk_and_embed(session, document, embedding_provider)

    extraction_provider = extraction_provider or get_extraction_provider()
    prompt_row = _get_or_create_prompt(session)

    def count_failures(candidate: ExtractionResult) -> int:
        if candidate.event is None:
            return 0
        return verify_evidence(candidate.event.evidence, document.clean_text).failed

    outcome = extract_document(
        provider=extraction_provider,
        source_name=source.name,
        publisher=source.publisher,
        published_at=document.published_at.isoformat() if document.published_at else "unknown",
        url=document.url,
        title=document.title or "",
        document_text=document.clean_text or "",
        verify_citations=count_failures,
        allow_escalation=allow_escalation,
    )
    result.extraction = outcome

    final = outcome.final
    if final is None or final.result is None:
        result.outcome = "error"
        result.reasons.append(final.error if final and final.error else "no extraction produced")
        _record_extractions(session, document, prompt_row, outcome, None, event_id=None)
        return result

    extracted = final.result

    if not extracted.in_niche:
        result.outcome = "background"
        result.reasons.append(extracted.abstention_reason or "out of niche")
        _record_extractions(session, document, prompt_row, outcome, None, event_id=None)
        log.info("document_out_of_niche", document_id=str(document.id))
        return result

    if extracted.abstained or extracted.event is None:
        # Abstention is a success path, recorded as such (CONSTRAINTS.md Section 6).
        result.outcome = "abstained"
        result.reasons.append(extracted.abstention_reason or "insufficient evidence")
        _record_extractions(session, document, prompt_row, outcome, None, event_id=None)
        log.info("document_abstained", document_id=str(document.id))
        return result

    event_data = extracted.event
    verification = verify_evidence(event_data.evidence, document.clean_text)
    result.verification = verification

    assessment = assess(
        event_data,
        verification,
        source_stores_full_text=source.store_full_text,
    )
    result.reasons.extend(assessment.reasons)

    if not verification.all_verified:
        # The hard gate. An unverified record is never written to `events`.
        result.outcome = "citation_failed"
        _record_extractions(session, document, prompt_row, outcome, verification, event_id=None)
        log.warning(
            "document_citations_failed",
            document_id=str(document.id),
            failed=verification.failed,
            checked=verification.checked,
        )
        return result

    vector = document_centroid(session, document.id)
    cluster, match = assign_to_cluster(session, vector, document.published_at)

    event = Event(
        cluster_id=cluster.id,
        event_type=event_data.event_type,
        event_date=event_data.event_date,
        title=event_data.title,
        summary=event_data.summary,
        severity=event_data.severity,
        severity_rationale=event_data.severity_rationale,
        confidence=assessment.confidence,
        requires_human_review=assessment.requires_human_review,
        review_status="pending" if assessment.requires_human_review else "not_required",
        status="active",
        change_note=None if match.is_new else "Update: new document matched an existing event.",
        first_seen_at=document.published_at or datetime.now(UTC),
        last_updated_at=document.published_at or datetime.now(UTC),
    )
    session.add(event)
    session.flush()

    for location in event_data.locations:
        session.add(
            EventLocation(
                event_id=event.event_id,
                name=location.name,
                location_type=location.location_type,
                country_code=location.country_code,
                location_precision=location.precision,
                geo_source="extraction",
            )
        )

    seen_actors: set[tuple[uuid.UUID, str]] = set()
    for actor in event_data.actors:
        entity = _get_or_create_entity(session, actor.name, actor.entity_type)
        key = (entity.id, actor.role)
        if key in seen_actors:
            continue
        seen_actors.add(key)
        session.add(EventActor(event_id=event.event_id, entity_id=entity.id, role=actor.role))

    for sector in dict.fromkeys(event_data.affected_sectors):
        session.add(EventSector(event_id=event.event_id, sector=sector))

    session.add(
        EventDocument(
            event_id=event.event_id,
            document_id=document.id,
            relation="new_event" if match.is_new else "update",
        )
    )

    verified_at = datetime.now(UTC)
    for quote in verification.quotes:
        session.add(
            EventEvidence(
                event_id=event.event_id,
                document_id=document.id,
                field_supported=quote.field_supported,
                quote=quote.quote,
                quote_char_start=quote.char_start,
                quote_char_end=quote.char_end,
                verified=quote.verified,
                verification_method=quote.method,
                verified_at=verified_at,
            )
        )

    _record_extractions(
        session, document, prompt_row, outcome, verification, event_id=event.event_id
    )
    session.flush()

    result.event_id = event.event_id
    result.outcome = "published" if not assessment.requires_human_review else "needs_review"
    log.info(
        "event_created",
        event_id=str(event.event_id),
        outcome=result.outcome,
        confidence=assessment.confidence,
        escalated=outcome.did_escalate,
        cost_usd=round(outcome.total_cost_usd, 6),
    )
    return result


def _record_extractions(
    session: Session,
    document: RawDocument,
    prompt: Prompt,
    outcome: ExtractionOutcome,
    verification: VerificationReport | None,
    event_id: uuid.UUID | None,
) -> None:
    """Write one ``extractions`` row per model call.

    Both halves of a cascade are recorded separately, so cost and quality can be
    attributed to the model that actually produced them (ADR-0002 D2).
    """
    final = outcome.final
    for call in outcome.calls:
        is_final = call is final
        outcome_label = _outcome_label(call, verification if is_final else None)
        session.add(
            Extraction(
                document_id=document.id,
                event_id=event_id if is_final else None,
                prompt_id=prompt.id,
                prompt_version=call.prompt_version,
                model_name=call.model,
                temperature=None,
                outcome=outcome_label,
                schema_valid=call.result is not None,
                citation_valid=(
                    verification.all_verified if is_final and verification is not None else None
                ),
                citations_checked=(
                    verification.checked if is_final and verification is not None else 0
                ),
                citations_failed=(
                    verification.failed if is_final and verification is not None else 0
                ),
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cache_write_tokens=call.cache_write_tokens,
                cache_read_tokens=call.cache_read_tokens,
                cost_usd=call.cost_usd,
                latency_ms=call.latency_ms,
                raw_output=call.result.model_dump(mode="json") if call.result else None,
                validation_errors={"error": call.error} if call.error else None,
            )
        )
    session.flush()


def _outcome_label(call: object, verification: VerificationReport | None) -> str:
    error = getattr(call, "error", None)
    result = getattr(call, "result", None)
    if error is not None:
        return "error"
    if result is None:
        return "schema_invalid"
    if not result.in_niche or result.abstained:
        return "abstained"
    if verification is not None and not verification.all_verified:
        return "citation_invalid"
    return "extracted"
