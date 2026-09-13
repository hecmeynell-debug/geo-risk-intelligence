"""Run the labelled set through the real pipeline, score it, and record the run.

Two callers, one code path (ADR-0006):

* ``scripts/run_evaluation.py`` with no flag builds a :class:`ScriptedExtractionProvider`
  per document from its hand-written expected output -- no key, no network, no spend --
  and scores the deterministic pipeline logic around a fixture "model".
* ``scripts/run_evaluation.py --live`` builds one real
  :class:`~gri.processing.extract.AnthropicExtractionProvider` and lets it see each
  document's raw text, scoring what the real model actually produced.

Either way, embedding stays on the deterministic fakes
(:class:`~gri.processing.embed.SameVectorProvider` / ``DeterministicFakeProvider``): which
documents cluster together is something this harness controls on purpose (an
``update``/``duplicate`` example must land against the right event to mean anything), not
something worth leaving to a semantic model's judgement call.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from gri.evaluation.dataset import (
    DATASET_NAME,
    DATASET_VERSION,
    SCENARIOS,
    EvalDocument,
    EvalScenario,
    scored_documents,
)
from gri.ingestion.normalise import content_hash, sha256_text
from gri.models import (
    EvaluationDataset,
    EvaluationExample,
    EvaluationResult,
    EvaluationRun,
    Event,
    RawDocument,
    Source,
)
from gri.processing.embed import DeterministicFakeProvider, SameVectorProvider
from gri.processing.extract import ExtractionProvider, ScriptedExtractionProvider
from gri.processing.pipeline import PipelineResult, process_document
from gri.processing.prompts import EXTRACT_EVENT_V2

#: Honest label for the deterministic tier's "model": it is a fixture, not a model, and
#: nothing that reads ``evaluation_runs.model_name`` should be able to mistake the two.
SCRIPTED_MODEL_LABEL = "scripted-fixture"


class ProviderFactory(Protocol):
    def __call__(self, document: EvalDocument) -> ExtractionProvider: ...


@dataclass
class ExampleOutcome:
    """What one scored document's run produced, and whether it matched expectation."""

    document: EvalDocument
    actual: dict[str, Any]
    mismatches: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: int = 0

    @property
    def passed(self) -> bool:
        return not self.mismatches


@dataclass
class HarnessRun:
    """Every scored outcome from one pass over :data:`gri.evaluation.dataset.SCENARIOS`."""

    outcomes: list[ExampleOutcome]
    model_label: str

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def all_passed(self) -> bool:
        return self.passed_count == self.total

    @property
    def total_cost_usd(self) -> float:
        return sum(o.cost_usd for o in self.outcomes)

    @property
    def total_latency_ms(self) -> int:
        return sum(o.latency_ms for o in self.outcomes)

    def by_label_type(self) -> dict[str, tuple[int, int]]:
        """``{label_type: (passed, total)}``, for a per-category breakdown."""
        counts: dict[str, list[int]] = {}
        for outcome in self.outcomes:
            bucket = counts.setdefault(outcome.document.label_type, [0, 0])
            bucket[1] += 1
            if outcome.passed:
                bucket[0] += 1
        return {label: (p, t) for label, (p, t) in counts.items()}

    def metrics(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed_count,
            "accuracy": (self.passed_count / self.total) if self.total else 0.0,
            "by_label_type": {
                label: {"passed": p, "total": t} for label, (p, t) in self.by_label_type().items()
            },
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_latency_ms": self.total_latency_ms,
        }


def _make_source(session: Session, slug: str) -> Source:
    source = Source(
        slug=f"eval-{slug}-{uuid.uuid4().hex[:8]}",
        name="Example Maritime Authority Notices",
        publisher="Example Maritime Authority",
        feed_url="https://notices.example.invalid/feed.xml",
        retrieval_method="rss",
        store_full_text=True,
        terms_reviewed_at=datetime.now(UTC),
        terms_reviewed_by="evaluation-harness",
        terms_url="https://notices.example.invalid/terms",
    )
    session.add(source)
    session.flush()
    return source


def _make_document(session: Session, source: Source, eval_doc: EvalDocument) -> RawDocument:
    marker = uuid.uuid4().hex
    text = eval_doc.text
    document = RawDocument(
        source_id=source.id,
        url=f"https://notices.example.invalid/eval/{marker}",
        title=eval_doc.title,
        published_at=datetime(2026, 9, 1, 8, 30, tzinfo=UTC),
        retrieved_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        retrieval_method="rss",
        clean_text=text,
        raw_body=text,
        raw_hash=sha256_text(text),
        content_hash=content_hash(text + marker),
    )
    session.add(document)
    session.flush()
    return document


def _actual_outcome(session: Session, result: PipelineResult) -> dict[str, Any]:
    event_created = result.event_id is not None
    requires_human_review: bool | None = None
    severity: str | None = None
    if event_created:
        event = session.get(Event, result.event_id)
        assert event is not None, "PipelineResult.event_id did not resolve to an event"
        requires_human_review = event.requires_human_review
        severity = event.severity
    return {
        "relation": result.relation,
        "outcome": result.outcome,
        "event_created": event_created,
        "requires_human_review": requires_human_review,
        "severity": severity,
    }


def _score(document: EvalDocument, actual: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    return {
        key: (expected, actual.get(key))
        for key, expected in document.expected.items()
        if actual.get(key) != expected
    }


def scripted_provider_factory(document: EvalDocument) -> ExtractionProvider:
    """The deterministic tier: each document gets its own hand-written expected output."""
    return ScriptedExtractionProvider([document.result])


def run_scenario(
    session: Session,
    scenario: EvalScenario,
    provider_factory: ProviderFactory,
) -> list[ExampleOutcome]:
    """Run every document in ``scenario`` in order, scoring the ones marked ``scored``.

    Runs inside a SAVEPOINT that is always rolled back before returning: the pipeline has
    to write real ``sources`` / ``raw_documents`` / ``events`` rows to actually run, but an
    evaluation run is telemetry, not data worth keeping -- and a synthetic "Example Port
    closed" event must never leak into the real feed, locations, or a briefing. Only the
    scored outcomes (plain data, read out before the rollback) survive the call.
    """
    outcomes: list[ExampleOutcome] = []
    nested = session.begin_nested()
    try:
        source = _make_source(session, scenario.key)
        embedding_provider = (
            SameVectorProvider() if scenario.same_cluster else DeterministicFakeProvider()
        )

        for eval_doc in scenario.documents:
            raw_document = _make_document(session, source, eval_doc)
            result = process_document(
                session,
                raw_document,
                embedding_provider=embedding_provider,
                extraction_provider=provider_factory(eval_doc),
            )
            if not eval_doc.scored:
                continue
            actual = _actual_outcome(session, result)
            calls = result.extraction.calls if result.extraction else []
            outcomes.append(
                ExampleOutcome(
                    document=eval_doc,
                    actual=actual,
                    mismatches=_score(eval_doc, actual),
                    cost_usd=sum(c.cost_usd for c in calls),
                    latency_ms=sum(c.latency_ms for c in calls),
                )
            )
    finally:
        nested.rollback()
    return outcomes


def run_all(session: Session, provider_factory: ProviderFactory, *, model_label: str) -> HarnessRun:
    outcomes: list[ExampleOutcome] = []
    for scenario in SCENARIOS:
        outcomes.extend(run_scenario(session, scenario, provider_factory))
    return HarnessRun(outcomes=outcomes, model_label=model_label)


def _git_sha() -> str | None:
    for env_var in ("GITHUB_SHA", "GIT_SHA"):
        if os.environ.get(env_var):
            return os.environ[env_var]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = completed.stdout.strip()
    return sha or None


def _get_or_create_dataset(session: Session) -> EvaluationDataset:
    dataset = session.scalar(
        select(EvaluationDataset).where(
            EvaluationDataset.name == DATASET_NAME, EvaluationDataset.version == DATASET_VERSION
        )
    )
    if dataset is None:
        dataset = EvaluationDataset(
            name=DATASET_NAME,
            version=DATASET_VERSION,
            description=(
                "ADR-0006's labelled set: synthetic documents covering every "
                "EVAL_LABEL_TYPES value."
            ),
        )
        session.add(dataset)
        session.flush()
    return dataset


def _sync_examples(session: Session, dataset: EvaluationDataset) -> dict[str, EvaluationExample]:
    """Create or refresh one ``EvaluationExample`` row per scored document.

    Idempotent and keyed on ``external_ref`` (the document's stable ``key``), so the
    database mirrors whatever is currently in :mod:`gri.evaluation.dataset` instead of
    drifting from it -- the same reasoning ``scripts/seed_sources.py`` applies to sources.
    """
    existing = {
        example.external_ref: example
        for example in session.scalars(
            select(EvaluationExample).where(EvaluationExample.dataset_id == dataset.id)
        )
        if example.external_ref
    }
    result: dict[str, EvaluationExample] = {}
    for doc in scored_documents():
        example = existing.get(doc.key)
        if example is None:
            example = EvaluationExample(
                dataset_id=dataset.id,
                external_ref=doc.key,
                label_type=doc.label_type,
                expected=doc.expected,
                notes=doc.notes,
            )
            session.add(example)
        else:
            example.label_type = doc.label_type
            example.expected = doc.expected
            example.notes = doc.notes
        result[doc.key] = example
    session.flush()
    return result


def record_run(session: Session, run: HarnessRun) -> EvaluationRun:
    """Persist ``run`` as an ``EvaluationRun`` with one ``EvaluationResult`` per outcome."""
    dataset = _get_or_create_dataset(session)
    examples_by_key = _sync_examples(session, dataset)

    started_at = datetime.now(UTC)
    eval_run = EvaluationRun(
        dataset_id=dataset.id,
        run_label=run.model_label,
        git_sha=_git_sha(),
        prompt_name=EXTRACT_EVENT_V2.name,
        prompt_version=EXTRACT_EVENT_V2.version,
        model_name=run.model_label,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        metrics=run.metrics(),
    )
    session.add(eval_run)
    session.flush()

    for outcome in run.outcomes:
        example = examples_by_key[outcome.document.key]
        session.add(
            EvaluationResult(
                run_id=eval_run.id,
                example_id=example.id,
                passed=outcome.passed,
                actual=outcome.actual,
                metrics={
                    "cost_usd": outcome.cost_usd,
                    "latency_ms": outcome.latency_ms,
                    "mismatches": {
                        k: {"expected": e, "actual": a} for k, (e, a) in outcome.mismatches.items()
                    },
                },
            )
        )
    session.flush()
    return eval_run
