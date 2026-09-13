# ADR-0006: The evaluation harness

- **Status:** Accepted
- **Date:** 2026-09-13
- **Phase:** 4 (Evaluation and operations)
- **Builds on:** [ADR-0001](ADR-0001-architecture-and-niche.md) D6 (prompt and model
  version recorded on every extraction), [ADR-0002](ADR-0002-embeddings-and-extraction.md)
  (the cascade, and cost/latency telemetry per call)

## Context

CONSTRAINTS.md Section 8 (Definition of Done) requires "evaluation metrics are measured
and visible" and "CI is green and fails on broken extraction or citation checks." The
`evaluation_datasets` / `evaluation_examples` / `evaluation_runs` / `evaluation_results`
tables have existed since Phase 0 for exactly this, unused until now.

Two things the harness must **not** do, because the project has already committed to
them elsewhere:

- **Run in CI with no key and no spend.** README: "The whole test suite runs against a
  scripted extraction provider: no key, no network, no spend." A CI-gated evaluation step
  that silently required `GRI_ANTHROPIC_API_KEY` would break every fork and every PR from
  a contributor who has not set one, and would spend money on every push.
- **Depend on an enabled source.** All ten candidate sources are disabled and unreviewed
  (NG-2); a dataset built from their live output would not exist yet, and building one
  from scraped real notices before their terms are reviewed would itself be an NG-2
  violation. `scripts/live_extraction_smoke.py` already established the pattern this ADR
  follows: synthetic, invented documents, clearly marked as such.

## Decision

**Two tiers, not one.**

1. **The deterministic gate** (`scripts/run_evaluation.py`, no flag). Runs a small,
   hand-built labelled set through the real pipeline (`process_document`: chunk, embed,
   cluster, extract, verify, score, route) using `ScriptedExtractionProvider` fed
   pre-written "model output" per document -- the same fixture pattern
   `tests/helpers.py` and `tests/factories.py` already use. No key, no network, no spend;
   runs on every push. This is a **pipeline regression gate**, not a model-quality
   measurement: the "model" here is a fixture, so a failure means the deterministic code
   around the model (routing, clustering, confidence scoring, the review triggers) broke,
   not that a real model's judgement changed. It is scored against
   `taxonomy.EVAL_LABEL_TYPES` (`new_event`, `update`, `duplicate`, `background`,
   `insufficient_evidence`) plus the review-routing outcome (`published` /
   `needs_review`), so the gate gives a wrong answer if the *routing logic* regresses,
   not only if the classification does.
2. **The live run** (`scripts/run_evaluation.py --live`). The same labelled documents,
   run through the real `AnthropicExtractionProvider` (`claude-sonnet-5`, escalating to
   `claude-opus-5`), scored the same way. This is genuine model-quality measurement and
   costs real money -- a human runs it deliberately, the way `live_extraction_smoke.py`
   is already run deliberately. **Never wired into CI.**

Both tiers write an `EvaluationRun` row (git SHA, prompt name/version, model name) and one
`EvaluationResult` per example, so a regression can be localised to a commit the way
ADR-0001 D6 already requires for extraction telemetry.

**Why the deterministic gate is allowed to be a hard, 100%-required gate**, unlike a
model-quality threshold: nothing about it is probabilistic. The "model" is a fixture that
returns exactly what the test wrote. If the pipeline is correct, every expected field
matches every time; there is no run-to-run variance to average over or tolerate. Any
mismatch is therefore a genuine regression in code that used to be correct, not sampling
noise -- so `scripts/run_evaluation.py` exits non-zero on the first tier's failure and CI
treats that exit code as a build failure, the same way `check_migrations_current.py` and
`check_prompts.py` already do.

**Why the live run is never a hard gate.** A real model's output shifts for reasons
outside this repository's control (a model or provider-side change), and CI running paid
calls on every push is both a cost and a reliability problem the project has already
ruled out. The live run's output is a report, not a gate: a human reads it before or
after a prompt change and decides what it means.

**Dataset scope for this PR.** Seven examples, chosen to cover every value of
`EVAL_LABEL_TYPES` at least once, plus both review-routing triggers the database
enforces (CONSTRAINTS.md Section 6, triggers 1 and 4):

| Example | `label_type` | Exercises |
|---|---|---|
| A well-evidenced closure, moderate severity, high confidence | `new_event` | The ordinary happy path: published, not flagged |
| The same shape, but severity `severe` | `new_event` | Trigger 4 -- high-impact severity always routes to review |
| The same shape, but confidence `0.55` | `new_event` | Trigger 1 -- confidence below 0.60 always routes to review |
| A follow-up notice with a materially new fact (longer delay estimate) against the first event | `update` | Consolidation recognises new information and re-evaluates review state (ADR-0004) |
| A follow-up notice that only restates the first event's facts | `duplicate` | Consolidation recognises corroboration and changes nothing |
| An unrelated grid-maintenance notice | `background` | Out-of-niche abstention (NG-1 boundary) |
| A thin, hedged notice with no confirmable facts | `insufficient_evidence` | Abstention is scored as success, not failure (CONSTRAINTS.md Section 6) |

A fabricated-citation case is deliberately **not** in this dataset: it is already the
named Phase 2 CI gate (`TestCitationGate` in `tests/test_pipeline_integration.py`), and
duplicating it here would test the same code path under a different name rather than add
coverage. The evaluation harness's job is classification and routing quality, not
re-proving the citation gate exists.

**Where the dataset lives.** `src/gri/evaluation/dataset.py`, as Python dataclasses, not
as rows seeded directly into `evaluation_examples`. The harness itself writes the
`EvaluationDataset` / `EvaluationExample` rows on every run (idempotently, keyed on
`(name, version)` for the dataset and a stable per-example key), so the database mirrors
whatever is currently in source control instead of drifting from it -- the same reasoning
`scripts/seed_sources.py` already applies to the source registry.

## Consequences

**Positive**

- CI gets a real regression gate over classification and review-routing logic, for free,
  on every push -- no key required, matching the project's existing no-spend-by-default
  posture.
- A deliberate, human-run path exists for measuring the actual model's quality, with cost
  and latency recorded per call (ADR-0001 D6), without ever risking an unattended spend.
- The labelled set is small enough to read end to end and verify by hand, which matters
  more than size while there is exactly one contributor curating it.

**Negative, accepted**

- Seven examples is not a statistically meaningful sample of model quality. The live tier
  reports numbers, but nobody should read "6/7 correct" as a quality percentage with any
  precision. Growing the dataset is expected, not a sign this PR is incomplete.
- The deterministic gate cannot catch a regression in the *real* model's judgement --
  only in the code around it. That is intentional (see Decision), but it means "CI is
  green" answers a narrower question than "the model is doing well," and the README and
  ADR both say so explicitly rather than letting the gate's name overclaim.
- `update` and `duplicate` examples need a shared event to attach to, so those two
  scenarios run two documents through the pipeline in sequence rather than one -- the
  harness supports a document sequence per example for exactly this reason, following the
  pattern already established in `tests/test_consolidation_integration.py`.
