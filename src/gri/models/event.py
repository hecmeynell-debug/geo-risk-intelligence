"""Event records, their evidence, and the human review trail.

Two constraints in this module carry most of the project's integrity guarantee:

* ``ck_events_high_severity_requires_review`` and
  ``ck_events_low_confidence_requires_review`` put CONSTRAINTS.md Section 6 triggers into
  the database, so a code path that forgets to set the flag cannot publish anyway.
* ``entities.entity_type`` excludes ``person`` (NG-4). See :mod:`gri.taxonomy`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gri.models.base import Base, created_at_col, one_of, uuid_pk
from gri.models.document import EMBEDDING_DIM
from gri.taxonomy import (
    ACTOR_ROLES,
    DOCUMENT_RELATIONS,
    ENTITY_TYPES,
    EVENT_STATUSES,
    EVENT_TYPES,
    LOCATION_PRECISIONS,
    LOCATION_TYPES,
    REVIEW_DECISIONS,
    REVIEW_STATUSES,
    SECTORS,
    SEVERITY_LEVELS,
)


class EventCluster(Base):
    """A group of documents believed to describe one real-world event.

    Clustering is what makes re-reporting tractable (ADR-0001 D7). Thresholds are chosen
    against the labelled set in Phase 2, not by intuition.
    """

    __tablename__ = "event_clusters"

    id: Mapped[uuid.UUID] = uuid_pk()
    label: Mapped[str | None] = mapped_column(Text)
    centroid: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    similarity_threshold: Mapped[float | None] = mapped_column(Numeric(4, 3))
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()

    events: Mapped[list[Event]] = relationship(back_populates="cluster")


class Event(Base):
    __tablename__ = "events"

    #: Named ``event_id`` rather than ``id`` because it is the public identifier exposed
    #: through the API and quoted in briefings.
    event_id: Mapped[uuid.UUID] = uuid_pk()

    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("event_clusters.id", ondelete="SET NULL")
    )

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    event_time_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: Grounded assembly of cited claims -- never free composition (NG-5).
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Must cite the evidence supporting the level (CONSTRAINTS.md Section 5).
    severity_rationale: Mapped[str | None] = mapped_column(Text)

    #: How well the record is supported by cited text -- NOT a probability the event
    #: occurred.
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))

    #: Defaults to true: a record is reviewable until something proves otherwise.
    requires_human_review: Mapped[bool] = mapped_column(nullable=False, default=True)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    #: "What is new since the last update" -- must itself be grounded in new evidence.
    change_note: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id", ondelete="SET NULL")
    )

    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()

    cluster: Mapped[EventCluster | None] = relationship(back_populates="events")
    locations: Mapped[list[EventLocation]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    evidence: Mapped[list[EventEvidence]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    actors: Mapped[list[EventActor]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    sectors: Mapped[list[EventSector]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    documents: Mapped[list[EventDocument]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    review_decisions: Mapped[list[ReviewDecision]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    __table_args__ = (
        one_of("event_type", EVENT_TYPES, "event_type"),
        one_of("severity", SEVERITY_LEVELS, "severity"),
        one_of("review_status", REVIEW_STATUSES, "review_status"),
        one_of("status", EVENT_STATUSES, "status"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_in_unit_interval",
        ),
        # CONSTRAINTS.md Section 6, trigger 4: high-impact claims always get a human.
        CheckConstraint(
            "severity NOT IN ('high', 'severe') OR requires_human_review",
            name="high_severity_requires_review",
        ),
        # CONSTRAINTS.md Section 6, trigger 1.
        CheckConstraint(
            "confidence IS NULL OR confidence >= 0.60 OR requires_human_review",
            name="low_confidence_requires_review",
        ),
        Index("ix_events_event_date", "event_date"),
        Index("ix_events_event_type", "event_type"),
        Index("ix_events_requires_human_review", "requires_human_review"),
    )


class EventLocation(Base):
    """A fixed feature or administrative area associated with an event.

    There is deliberately no position time-series here: locations describe ports,
    straits, terminals and corridors, never the movement of an individual entity (NG-4).
    """

    __tablename__ = "event_locations"

    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("events.event_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    location_type: Mapped[str] = mapped_column(String(32), nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(2))
    admin_area: Mapped[str | None] = mapped_column(Text)

    latitude: Mapped[float | None] = mapped_column(Numeric(8, 5))
    longitude: Mapped[float | None] = mapped_column(Numeric(8, 5))
    #: Coarseness of the coordinate, so the map can refuse to imply precision it lacks.
    location_precision: Mapped[str | None] = mapped_column(String(32))
    geo_source: Mapped[str | None] = mapped_column(String(128))

    event: Mapped[Event] = relationship(back_populates="locations")

    __table_args__ = (
        one_of("location_type", LOCATION_TYPES, "location_type"),
        one_of("location_precision", LOCATION_PRECISIONS, "location_precision"),
        CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name="latitude_range",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="longitude_range",
        ),
        CheckConstraint(
            "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'",
            name="country_code_is_iso_alpha2",
        ),
    )


class Entity(Base):
    """An organisation, state, facility, or vessel named by a source.

    NG-4 enforcement: ``entity_type`` has no ``person`` member, so person-level records
    are unrepresentable rather than merely discouraged.
    """

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = uuid_pk()
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(2))
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()

    event_links: Mapped[list[EventActor]] = relationship(back_populates="entity")

    __table_args__ = (
        UniqueConstraint(
            "canonical_name", "entity_type", name="uq_entities_canonical_name_entity_type"
        ),
        one_of("entity_type", ENTITY_TYPES, "entity_type"),
    )


class EventActor(Base):
    __tablename__ = "event_actors"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), primary_key=True)

    event: Mapped[Event] = relationship(back_populates="actors")
    entity: Mapped[Entity] = relationship(back_populates="event_links")

    __table_args__ = (one_of("role", ACTOR_ROLES, "role"),)


class EventSector(Base):
    __tablename__ = "event_sectors"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id", ondelete="CASCADE"), primary_key=True
    )
    sector: Mapped[str] = mapped_column(String(32), primary_key=True)

    event: Mapped[Event] = relationship(back_populates="sectors")

    __table_args__ = (one_of("sector", SECTORS, "sector"),)


class EventDocument(Base):
    """Links an event to a document, recording how that document related to it."""

    __tablename__ = "event_documents"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_documents.id", ondelete="CASCADE"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    first_linked_at: Mapped[datetime] = created_at_col()

    event: Mapped[Event] = relationship(back_populates="documents")

    __table_args__ = (one_of("relation", DOCUMENT_RELATIONS, "relation"),)


class EventEvidence(Base):
    """A quote from a source document supporting a specific field of an event.

    This table is the mechanical form of NG-3 and NG-5. ``verified = true`` means the
    exact quote string was located in the stored ``clean_text`` of the referenced
    document by deterministic code -- not that a model asserted it.
    """

    __tablename__ = "event_evidence"

    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("events.event_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_documents.id", ondelete="RESTRICT"), nullable=False
    )

    #: Which event field this quote supports, e.g. "severity", "event_date", "locations".
    field_supported: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The exact quote, as it appears in the document's ``clean_text``.
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_char_start: Mapped[int | None] = mapped_column(Integer)
    quote_char_end: Mapped[int | None] = mapped_column(Integer)

    verified: Mapped[bool] = mapped_column(nullable=False, default=False)
    verification_method: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = created_at_col()

    event: Mapped[Event] = relationship(back_populates="evidence")

    __table_args__ = (
        CheckConstraint("length(btrim(quote)) > 0", name="quote_not_blank"),
        CheckConstraint(
            "quote_char_start IS NULL OR quote_char_end IS NULL"
            " OR quote_char_end > quote_char_start",
            name="offsets_ordered",
        ),
        # A verified citation must say how it was verified and when.
        CheckConstraint(
            "NOT verified OR (verification_method IS NOT NULL AND verified_at IS NOT NULL)",
            name="verified_records_method",
        ),
    )


class ReviewDecision(Base):
    """An append-only record of a human decision on an event."""

    __tablename__ = "review_decisions"

    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("events.event_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    reviewer: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    #: A decision without a reason is not auditable, so the reason is mandatory.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    changes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at_col()

    event: Mapped[Event] = relationship(back_populates="review_decisions")

    __table_args__ = (
        one_of("decision", REVIEW_DECISIONS, "decision"),
        CheckConstraint("length(btrim(reason)) > 0", name="reason_not_blank"),
    )
