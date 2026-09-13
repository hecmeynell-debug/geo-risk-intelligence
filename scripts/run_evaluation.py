"""Run the Phase 4 evaluation harness (ADR-0006).

**Default (no flag): the deterministic gate.** Runs the labelled set
(``gri.evaluation.dataset``) through the real pipeline with a scripted "model" built from
each document's hand-written expected output. No key, no network, no spend -- this is
what CI runs on every push. Any mismatch is a genuine regression in the deterministic
code around the model (routing, clustering, confidence scoring, the review triggers),
because the "model" here is a fixture with no run-to-run variance to allow for. Exits
non-zero on any mismatch, and CI treats that as a build failure.

**``--live``: real model quality, never run in CI.** The same labelled documents, run
through the real ``claude-sonnet-5`` / ``claude-opus-5`` cascade. **This costs money.**
Reports what the model actually produced against the same expectations, but never fails
the build: a real model's output is not something this repository controls run to run,
so this is a report for a human to read, not a gate.

Usage::

    docker compose run --rm migrate python scripts/run_evaluation.py
    docker compose run --rm migrate python scripts/run_evaluation.py --live
"""

from __future__ import annotations

import argparse
import sys

from gri.config import get_settings
from gri.db import session_scope
from gri.evaluation.dataset import EvalDocument
from gri.evaluation.harness import (
    SCRIPTED_MODEL_LABEL,
    ExampleOutcome,
    HarnessRun,
    record_run,
    run_all,
    scripted_provider_factory,
)
from gri.processing.extract import (
    DEFAULT_MODEL,
    ESCALATION_MODEL,
    AnthropicExtractionProvider,
    ExtractionProvider,
)


def _print_outcome(outcome: ExampleOutcome) -> None:
    mark = "PASS" if outcome.passed else "FAIL"
    doc = outcome.document
    print(f"  [{mark}] {doc.key} ({doc.label_type})")
    for field_name, (expected, actual) in outcome.mismatches.items():
        print(f"        {field_name}: expected {expected!r}, got {actual!r}")


def _run(*, live: bool) -> HarnessRun:
    with session_scope() as session:
        if live:
            settings = get_settings()
            if not settings.anthropic_api_key:
                print(
                    "no GRI_ANTHROPIC_API_KEY set. The SDK may still resolve "
                    "ANTHROPIC_API_KEY or an `ant auth login` profile; if this fails, set "
                    "GRI_ANTHROPIC_API_KEY in .env.",
                    file=sys.stderr,
                )
            provider = AnthropicExtractionProvider(api_key=settings.anthropic_api_key)

            def live_factory(_doc: EvalDocument) -> ExtractionProvider:
                # One real provider, reused for every document: what is being measured
                # is the real model's output, not a fresh instance per call.
                return provider

            run = run_all(
                session, live_factory, model_label=f"{DEFAULT_MODEL} -> {ESCALATION_MODEL} cascade"
            )
        else:
            run = run_all(session, scripted_provider_factory, model_label=SCRIPTED_MODEL_LABEL)

        record_run(session, run)
        # Per-scenario pipeline scaffolding (sources, documents, events) is already
        # discarded inside run_scenario's SAVEPOINT; what session_scope commits here is
        # only the EvaluationDataset/Example/Run/Result rows record_run just wrote.
    return run


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "use the real Anthropic API instead of the scripted provider "
            "(costs money; never run in CI)"
        ),
    )
    args = parser.parse_args()

    mode = "LIVE (real model, costs money)" if args.live else "deterministic (scripted, no spend)"
    print(f"Evaluation harness: {mode}")
    print("-" * 78)

    run = _run(live=args.live)

    for outcome in run.outcomes:
        _print_outcome(outcome)

    print("-" * 78)
    for label, (passed, total) in sorted(run.by_label_type().items()):
        print(f"  {label:<24} {passed}/{total}")
    print("-" * 78)
    print(f"Total:        {run.passed_count}/{run.total} passed")
    if run.total_cost_usd:
        print(f"Total cost:   ${run.total_cost_usd:.6f}")
    if run.total_latency_ms:
        print(f"Total latency: {run.total_latency_ms} ms")

    if args.live:
        print("\nLive run: reported, not gated (ADR-0006). Exiting 0 regardless of outcome.")
        return 0

    if not run.all_passed:
        print("\nDeterministic gate FAILED: this is a regression, not model variance.")
        return 1

    print("\nDeterministic gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
