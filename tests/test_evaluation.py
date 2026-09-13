"""The Phase 4 evaluation harness (ADR-0006).

``TestDataset`` needs no database: it pins the labelled set's own internal consistency.
``TestHarness`` runs the deterministic tier against a live Postgres, the same way
``scripts/run_evaluation.py`` does, including the property that matters most: an
evaluation run must never leave synthetic data behind for the feed, locations, or a
briefing to pick up.

Run with:  pytest -m integration
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gri.evaluation.dataset import SCENARIOS, scored_documents
from gri.evaluation.harness import record_run, run_all, run_scenario, scripted_provider_factory
from gri.models import EvaluationDataset, EvaluationExample, EvaluationResult, Event, Source
from gri.taxonomy import EVAL_LABEL_TYPES

integration = pytest.mark.integration


class TestDataset:
    def test_every_scenario_key_is_unique(self) -> None:
        keys = [scenario.key for scenario in SCENARIOS]
        assert len(keys) == len(set(keys))

    def test_every_scored_document_key_is_unique(self) -> None:
        keys = [doc.key for doc in scored_documents()]
        assert len(keys) == len(set(keys))

    def test_scored_documents_cover_every_label_type_at_least_once(self) -> None:
        labels = {doc.label_type for doc in scored_documents()}
        assert labels == EVAL_LABEL_TYPES

    def test_every_expected_dict_has_the_same_shape(self) -> None:
        """A harness that only checks whichever keys happen to be present would silently
        stop checking a field a future edit forgot to set -- so every example must set
        exactly the same keys, with ``None`` standing in for 'not applicable'."""
        shapes = {frozenset(doc.expected.keys()) for doc in scored_documents()}
        assert len(shapes) == 1

    def test_setup_documents_are_not_scored(self) -> None:
        """The update/duplicate scenarios' first document sets up the event the second
        reports against; ADR-0006 counts it as scaffolding, not a measured example."""
        setup_docs = [doc for scenario in SCENARIOS for doc in scenario.documents if not doc.scored]
        assert setup_docs
        for doc in setup_docs:
            assert doc not in scored_documents()


@integration
class TestHarness:
    def test_the_deterministic_tier_passes_every_example(self, session: Session) -> None:
        run = run_all(session, scripted_provider_factory, model_label="scripted-fixture")

        assert run.total == len(scored_documents())
        assert run.all_passed, [
            (o.document.key, o.mismatches) for o in run.outcomes if not o.passed
        ]
        assert set(run.by_label_type()) == EVAL_LABEL_TYPES

    def test_a_scenario_leaves_no_data_behind(self, session: Session) -> None:
        """The core guarantee: a synthetic 'Example Port closed' event must never leak
        into the real feed, locations, or a briefing (ADR-0006)."""
        before_sources = session.scalar(select(func.count()).select_from(Source)) or 0
        before_events = session.scalar(select(func.count()).select_from(Event)) or 0

        outcomes = run_scenario(session, SCENARIOS[0], scripted_provider_factory)
        assert outcomes  # the scenario did run and did score something

        after_sources = session.scalar(select(func.count()).select_from(Source)) or 0
        after_events = session.scalar(select(func.count()).select_from(Event)) or 0
        assert after_sources == before_sources
        assert after_events == before_events

    def test_record_run_persists_one_result_per_scored_example(self, session: Session) -> None:
        run = run_all(session, scripted_provider_factory, model_label="scripted-fixture")

        eval_run = record_run(session, run)

        results = session.scalars(
            select(EvaluationResult).where(EvaluationResult.run_id == eval_run.id)
        ).all()
        assert len(results) == len(scored_documents())
        assert all(r.passed for r in results)
        assert eval_run.metrics["accuracy"] == 1.0

    def test_record_run_is_idempotent_on_the_dataset_and_examples(self, session: Session) -> None:
        """Two runs must not double the dataset or its examples -- only add a second
        ``EvaluationRun`` -- so the database mirrors the current dataset, not its history
        of having been recorded before (ADR-0006)."""
        run = run_all(session, scripted_provider_factory, model_label="scripted-fixture")

        record_run(session, run)
        second = run_all(session, scripted_provider_factory, model_label="scripted-fixture")
        record_run(session, second)

        datasets = session.scalars(select(EvaluationDataset)).all()
        assert len(datasets) == 1
        examples = session.scalars(
            select(EvaluationExample).where(EvaluationExample.dataset_id == datasets[0].id)
        ).all()
        assert len(examples) == len(scored_documents())
