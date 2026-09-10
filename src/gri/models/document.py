"""Raw document store and chunks.

``raw_documents`` is the *primary* record of the system (ADR-0001 D2). Events are derived
from it, and citation verification runs against ``clean_text`` -- the text exactly as we
retrieved it, not as the publisher may later have edited it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gri.models.base import Base, created_at_col, one_of, sha256_hex, uuid_pk
from gri.taxonomy import RETRIEVAL_METHODS

if TYPE_CHECKING:
    from gri.models.source import IngestionRun, Source

#: Provisional. ADR-0001 leaves the embedding model open until Phase 2; each row records
#: the model and dimension actually used so a mismatch is detectable rather than silent.
EMBEDDING_DIM = 1536


class RawDocument(Base):
    __tablename__ = "raw_documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    fetch_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )

    #: The publisher's own identifier -- feed GUID, notice number, filing id.
    source_uid: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)

    #: Publication time as stated by the source. Deliberately distinct from
    #: ``retrieved_at``: conflating them destroys reporting-lag analysis.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Ingestion time -- when we actually fetched it.
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    retrieval_method: Mapped[str] = mapped_column(String(32), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(128))
    etag: Mapped[str | None] = mapped_column(String(256))
    last_modified: Mapped[str | None] = mapped_column(String(128))

    #: Exact bytes retrieved, kept for audit. Null when the source's terms permit only
    #: metadata retention (``sources.store_full_text = false``).
    raw_body: Mapped[str | None] = mapped_column(Text)
    #: Normalised text. Citation verification matches against THIS column.
    clean_text: Mapped[str | None] = mapped_column(Text)

    #: SHA-256 of the raw response bytes.
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: SHA-256 of the normalised text; the deduplication key.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    language: Mapped[str | None] = mapped_column(String(16))

    #: Content removed on publisher request; provenance and hashes retained so the audit
    #: trail survives without retaining the content.
    is_tombstoned: Mapped[bool] = mapped_column(nullable=False, default=False)

    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()

    source: Mapped[Source] = relationship(back_populates="documents")
    fetch_run: Mapped[IngestionRun | None] = relationship(back_populates="documents")
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Document-level idempotency: re-ingesting the same item is a no-op.
        UniqueConstraint("source_id", "content_hash", name="uq_raw_documents_source_content_hash"),
        Index("ix_raw_documents_source_id_source_uid", "source_id", "source_uid"),
        Index("ix_raw_documents_published_at", "published_at"),
        Index("ix_raw_documents_retrieved_at", "retrieved_at"),
        one_of("retrieval_method", RETRIEVAL_METHODS, "retrieval_method"),
        sha256_hex("raw_hash", "raw_hash_is_sha256"),
        sha256_hex("content_hash", "content_hash_is_sha256"),
        CheckConstraint(
            "NOT is_tombstoned OR (raw_body IS NULL AND clean_text IS NULL)",
            name="tombstoned_has_no_content",
        ),
    )


class DocumentChunk(Base):
    """A passage of a document, with its embedding. Chunks carry character offsets back
    into ``clean_text`` so a citation can be located precisely."""

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_documents.id", ondelete="CASCADE"), nullable=False
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_dim: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = created_at_col()

    document: Mapped[RawDocument] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_document_chunks_document_id_chunk_index"
        ),
        CheckConstraint("char_end > char_start", name="offsets_ordered"),
        CheckConstraint("char_start >= 0", name="offsets_non_negative"),
        CheckConstraint(
            "embedding IS NULL OR embedding_model IS NOT NULL",
            name="embedding_records_model",
        ),
    )
