"""Evaluation datasets, runs, and per-example results.

Phase 4 builds the labelled set and the CI gate. The tables exist from Phase 0 so that
the schema does not have to churn later, and so that evaluation results are stored in the
same database as the artefacts they measure.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gri.models.base import Base, created_at_col, one_of, uuid_pk
from gri.taxonomy import EVAL_LABEL_TYPES


class EvaluationDataset(Base):
    __tablename__ = "evaluation_datasets"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    examples: Mapped[list[EvaluationExample]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )
    runs: Mapped[list[EvaluationRun]] = relationship(back_populates="dataset")

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_evaluation_datasets_name_version"),
    )


class EvaluationExample(Base):
    """One labelled document. ``expected`` holds the hand-written ground truth.

    The label set includes ``duplicate`` and ``insufficient_evidence`` because correct
    abstention and correct deduplication are things we intend to measure, not incidental
    behaviours.
    """

    __tablename__ = "evaluation_examples"

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_documents.id", ondelete="SET NULL")
    )
    #: Stable reference to the fixture on disk when the document is not in the database.
    external_ref: Mapped[str | None] = mapped_column(String(512))

    label_type: Mapped[str] = mapped_column(String(32), nullable=False)
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    dataset: Mapped[EvaluationDataset] = relationship(back_populates="examples")
    results: Mapped[list[EvaluationResult]] = relationship(back_populates="example")

    __table_args__ = (one_of("label_type", EVAL_LABEL_TYPES, "label_type"),)


class EvaluationRun(Base):
    """One execution of the harness against one dataset version.

    The prompt version, model, and git SHA are recorded so a regression can be localised
    to a change rather than merely observed.
    """

    __tablename__ = "evaluation_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_datasets.id", ondelete="RESTRICT"),
        nullable=False,
    )

    run_label: Mapped[str | None] = mapped_column(String(200))
    git_sha: Mapped[str | None] = mapped_column(String(40))
    prompt_name: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(128))

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Aggregate metrics: schema-valid rate, citation validity, duplicate-detection
    #: quality, precision/recall proxies, latency, cost per document, acceptance rate.
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()

    dataset: Mapped[EvaluationDataset] = relationship(back_populates="runs")
    results: Mapped[list[EvaluationResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False
    )
    example_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_examples.id", ondelete="CASCADE"), nullable=False
    )

    passed: Mapped[bool] = mapped_column(nullable=False)
    actual: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()

    run: Mapped[EvaluationRun] = relationship(back_populates="results")
    example: Mapped[EvaluationExample] = relationship(back_populates="results")

    __table_args__ = (
        UniqueConstraint("run_id", "example_id", name="uq_evaluation_results_run_id_example_id"),
    )
