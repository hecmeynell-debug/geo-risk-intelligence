"""Chunking, embedding, clustering, extraction, and citation verification.

Pipeline order, which is load-bearing rather than incidental:

    chunk -> embed -> cluster -> extract -> validate schema -> VERIFY CITATIONS
          -> score confidence -> publish OR route to human review

``verify`` sits between the model and the database. Nothing reaches the ``events`` table
without passing it.
"""

from __future__ import annotations

from gri.processing.chunk import Chunk, chunk_text, verify_offsets
from gri.processing.cluster import (
    SIMILARITY_THRESHOLD,
    TIME_WINDOW,
    assign_to_cluster,
    document_centroid,
    find_matching_cluster,
)
from gri.processing.confidence import REVIEW_THRESHOLD, ConfidenceAssessment, assess
from gri.processing.embed import (
    DeterministicFakeProvider,
    EmbeddingProvider,
    FastEmbedProvider,
    centroid,
    cosine_similarity,
)
from gri.processing.extract import (
    DEFAULT_MODEL,
    ESCALATION_MODEL,
    ExtractionOutcome,
    ExtractionProvider,
    ScriptedExtractionProvider,
    compute_cost_usd,
    extract_document,
    should_escalate,
)
from gri.processing.pipeline import PipelineResult, chunk_and_embed, process_document
from gri.processing.verify import VerificationReport, VerifiedQuote, verify_evidence, verify_quote

__all__ = [
    "DEFAULT_MODEL",
    "ESCALATION_MODEL",
    "REVIEW_THRESHOLD",
    "SIMILARITY_THRESHOLD",
    "TIME_WINDOW",
    "Chunk",
    "ConfidenceAssessment",
    "DeterministicFakeProvider",
    "EmbeddingProvider",
    "ExtractionOutcome",
    "ExtractionProvider",
    "FastEmbedProvider",
    "PipelineResult",
    "ScriptedExtractionProvider",
    "VerificationReport",
    "VerifiedQuote",
    "assess",
    "assign_to_cluster",
    "centroid",
    "chunk_and_embed",
    "chunk_text",
    "compute_cost_usd",
    "cosine_similarity",
    "document_centroid",
    "extract_document",
    "find_matching_cluster",
    "process_document",
    "should_escalate",
    "verify_evidence",
    "verify_offsets",
    "verify_quote",
]
