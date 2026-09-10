"""Prompt registry and per-extraction telemetry.

ADR-0001 D6: an evaluation number that cannot be attributed to a specific prompt and
model version is not a measurement. Every extraction records both, plus tokens, cost and
latency, so cost-per-document is measured rather than estimated.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gri.models.base import Base, created_at_col, one_of, sha256_hex, uuid_pk
from gri.taxonomy import EXTRACTION_OUTCOMES


class Prompt(Base):
    """A versioned prompt template. Rows are immutable: a change is a new version."""

    __tablename__ = "prompts"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    #: SHA-256 of ``template``; lets CI detect an edited prompt that kept its version.
    template_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_at_col()

    extractions: Mapped[list[Extraction]] = relationship(back_populates="prompt")

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_prompts_name_version"),
        sha256_hex("template_hash", "template_hash_is_sha256"),
    )


class Extraction(Base):
    """One attempt to turn one document into a structured record."""

    __tablename__ = "extractions"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raw_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id", ondelete="SET NULL")
    )
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompts.id", ondelete="RESTRICT"), nullable=False
    )

    #: Denormalised from ``prompts`` on purpose: telemetry must stay readable even if the
    #: prompt row is later reorganised.
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(128))
    temperature: Mapped[float | None] = mapped_column(Numeric(3, 2))

    #: ``abstained`` is a success, not an error (CONSTRAINTS.md Section 6).
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_valid: Mapped[bool | None] = mapped_column()
    citation_valid: Mapped[bool | None] = mapped_column()
    citations_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    citations_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    raw_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validation_errors: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = created_at_col()

    prompt: Mapped[Prompt] = relationship(back_populates="extractions")

    __table_args__ = (
        one_of("outcome", EXTRACTION_OUTCOMES, "outcome"),
        CheckConstraint(
            "citations_failed <= citations_checked", name="ck_extractions_failed_le_checked"
        ),
        CheckConstraint("citations_checked >= 0", name="ck_extractions_citations_non_negative"),
        # An extraction that produced an event must have passed both gates.
        CheckConstraint(
            "event_id IS NULL OR (schema_valid AND citation_valid)",
            name="ck_extractions_published_requires_valid_schema_and_citations",
        ),
    )
