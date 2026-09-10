"""Source registry and ingestion run bookkeeping.

The ``sources`` table is where NG-2 is enforced: a source cannot be enabled, and its full
text cannot be stored, until a human has recorded a terms review. See
``docs/source-provenance-policy.md``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gri.models.base import Base, created_at_col, one_of, updated_at_col, uuid_pk
from gri.taxonomy import INGESTION_RUN_STATUSES, RETRIEVAL_METHODS

if TYPE_CHECKING:
    from gri.models.document import RawDocument


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = uuid_pk()

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    publisher: Mapped[str] = mapped_column(String(200), nullable=False)
    homepage_url: Mapped[str | None] = mapped_column(Text)
    feed_url: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_method: Mapped[str] = mapped_column(String(32), nullable=False)

    #: 30-60 minutes, per CONSTRAINTS.md. Enforced by CHECK so a well-meaning config
    #: change cannot turn this into aggressive polling of a public authority.
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1800)

    #: Adapters ship disabled. Enabling requires a recorded terms review.
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False)

    # -- Terms review (the NG-2 gate) ---------------------------------------------------
    terms_url: Mapped[str | None] = mapped_column(Text)
    terms_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_reviewed_by: Mapped[str | None] = mapped_column(String(200))
    terms_note: Mapped[str | None] = mapped_column(Text)
    licence: Mapped[str | None] = mapped_column(String(200))
    robots_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: False means we may store only headline, link, publication time and metadata --
    #: and therefore this source can never produce quoted evidence.
    store_full_text: Mapped[bool] = mapped_column(nullable=False, default=False)

    # -- Verification of the adapter itself ---------------------------------------------
    #: A human confirmed this endpoint is correct and still exists.
    endpoint_verified: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    #: A human looked at a real response and confirmed the adapter config parses it.
    #: Without this, an enabled source would be running on a guessed field mapping.
    format_confirmed: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )

    # -- Fetch state, for conditional requests and backoff ------------------------------
    last_etag: Mapped[str | None] = mapped_column(String(256))
    last_modified_header: Mapped[str | None] = mapped_column(String(128))
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Set when the runner disables a source itself, e.g. after a block.
    disabled_reason: Mapped[str | None] = mapped_column(Text)

    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    documents: Mapped[list[RawDocument]] = relationship(back_populates="source")
    runs: Mapped[list[IngestionRun]] = relationship(back_populates="source")

    __table_args__ = (
        one_of("retrieval_method", RETRIEVAL_METHODS, "retrieval_method"),
        CheckConstraint(
            "poll_interval_seconds BETWEEN 1800 AND 3600",
            name="poll_interval_30_to_60_min",
        ),
        # NG-2: no enabling a source nobody has reviewed the terms for.
        CheckConstraint(
            "NOT enabled OR terms_reviewed_at IS NOT NULL",
            name="enabled_requires_terms_review",
        ),
        # NG-2: no storing full body text without a review that permits it.
        CheckConstraint(
            "NOT store_full_text OR terms_reviewed_at IS NOT NULL",
            name="full_text_requires_terms_review",
        ),
        # An enabled source must be running on a confirmed endpoint and a confirmed
        # parse, not on the registry's starting hypothesis.
        CheckConstraint(
            "NOT enabled OR (endpoint_verified AND format_confirmed)",
            name="enabled_requires_verified_adapter",
        ),
        CheckConstraint("consecutive_failures >= 0", name="consecutive_failures_non_negative"),
    )


class IngestionRun(Base):
    """One scheduled fetch of one source. Every document links back to the run that
    retrieved it, so any row can be traced to a specific fetch."""

    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")

    items_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    source: Mapped[Source] = relationship(back_populates="runs")
    documents: Mapped[list[RawDocument]] = relationship(back_populates="fetch_run")

    __table_args__ = (one_of("status", INGESTION_RUN_STATUSES, "status"),)
