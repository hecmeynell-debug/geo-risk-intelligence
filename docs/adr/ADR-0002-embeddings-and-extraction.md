# ADR-0002: Embedding model, extraction model, and the verification boundary

- **Status:** Accepted
- **Date:** 2026-09-10
- **Phase:** 2 (Core Intelligence Pipeline)
- **Closes:** the three open questions left by [ADR-0001](ADR-0001-architecture-and-niche.md)

---

## Context

ADR-0001 deliberately deferred three choices until there was a pipeline to measure them
against: the embedding model and its dimension, the clustering thresholds, and the
extraction model. Phase 2 has to settle them because they are load-bearing for the
Definition of Done: cost per document, schema-valid rate, and citation validity are all
attributes of these choices.

---

## Decision

### D1. Embeddings: `BAAI/bge-small-en-v1.5`, run locally, 384 dimensions

Chunk embeddings are produced in-process by [fastembed](https://github.com/qdrant/fastembed),
which runs the model on `onnxruntime` — no PyTorch, no GPU, no API call.

**Why local rather than a hosted embedding API:**

- **The evaluation harness stays runnable.** Phase 4 has to re-run 50–100 documents on
  every prompt or threshold change, in CI. An embedding API would mean either a CI secret
  and real spend on every push, or skipping the embedding half of the harness. Neither is
  acceptable for a gate that is supposed to fail loudly.
- **One-command reproducibility.** The Definition of Done requires a clean checkout to
  come up with `docker compose up`. A second vendor and a second key works against that.
- **NG-7.** Embedding a few thousand short notices is not a problem that needs a hosted
  service.
- Anthropic does not offer an embeddings API, so a hosted choice would have meant adding
  a *second* provider alongside the extraction model. One vendor relationship is simpler.

**Cost of this decision, stated plainly:** `bge-small` is a small model. It is strong on
short, factual, English text — which is what maritime and energy notices are — but it is
weaker than a large hosted embedding model on paraphrase and on non-English source text.
Our corpus is overwhelmingly English official notices, so this is a good trade today; it
would be a bad trade if the niche expanded to multilingual reporting, which NG-6 says it
will not.

**Dimension changes from 1536 to 384.** ADR-0001 provisionally sized the column at 1536
and required every row to record `embedding_model` and `embedding_dim` so a mismatch is
detectable. Migration `b49835e1be30` narrows `document_chunks.embedding` and
`event_clusters.centroid` to `vector(384)`. There is no data to migrate — no source has
ever been enabled — so this is a schema change rather than a re-embed. **Once real
documents exist, a dimension change stops being cheap**: it requires re-embedding the
whole corpus, and the migration would have to do that explicitly.

The model is baked into the image at build time rather than downloaded on first use, so
the worker starts deterministically and works offline. The migration also refuses to run
if any vectors already exist, so a future dimension change cannot silently leave the
corpus unsearchable.

### D2. Extraction: Claude Sonnet 5, escalating to Claude Opus 5

`claude-sonnet-5` handles every document. A second pass on `claude-opus-5` is triggered
when — and only when — the first pass is weak:

- confidence below the 0.60 review threshold, **or**
- one or more citations failed verification, **or**
- the model abstained but the document scored as clearly in-niche

**Why a cascade rather than one model:**

The extraction task is mostly mechanical — pull dates, places, and operators out of a
notice that states them plainly — and Sonnet 5 is strong at schema-constrained output at
a fifth of Opus 5's input cost. The genuinely hard cases are the ones that matter for
this project's thesis: hedged language, conflicting reports, and evidence that does not
support the obvious conclusion. Spending Opus on exactly those, and only those, puts the
capability where the judgement is needed.

**Cost of this decision, stated plainly:** the cascade adds a second code path that has
to be evaluated in its own right. Phase 4 measures the escalation rate, and reports
metrics split by which model produced the record — otherwise an aggregate number would
hide whether Sonnet or Opus is carrying the quality. If the escalation rate turns out
high, the cascade is not earning its complexity and we should collapse to one model. That
is a measurement, not a guess, and the threshold for revisiting is recorded here: **if
more than ~30% of documents escalate, drop the cascade.**

Both passes are recorded as separate `extractions` rows with their own model, prompt
version, tokens, latency, and cost, so cost-per-document is measured rather than
estimated (ADR-0001 D6).

### D3. Schema-constrained output via `messages.parse`, not prompt instructions

Extraction uses the Anthropic SDK's `client.messages.parse(output_format=...)` with a
Pydantic model. The API constrains generation to the schema and the SDK returns a
validated instance.

**Why this rather than "return JSON" in the prompt:** asking a model to emit JSON and
then parsing it is a guess with a retry loop attached. Constrained decoding makes
schema-invalid output a non-event, which turns "schema-valid rate" from the thing we hope
for into a metric that should sit at 100% and whose *deviation* is the signal.

Note that this makes the schema-valid rate a weak quality signal on its own. It says the
shape is right, not that the content is. Citation validity is the metric that carries
weight, which is D4.

### D4. Citation verification stays deterministic and outside the model

Verification is a normalised substring search of each quote against the stored
`clean_text`, using the same `gri.ingestion.normalise` functions that produced it. It is
not a model call and never will be.

Matching is done on normalised text on both sides, so a quote that differs from the
source only in whitespace or Unicode compatibility form still verifies, while a quote
with different *words* does not. Character offsets are recorded against the normalised
text so the UI can highlight the passage.

**What this catches:** fabricated quotes, and quotes silently attributed to the wrong
document.
**What it does not catch:** a real quote attached to a wrong conclusion. That is the
residual risk ADR-0001 already named, and it is what the confidence score, the mandatory
review of `high`/`severe` records, and the human queue exist to absorb. We would rather
state this than imply the check is stronger than it is.

### D5. Clustering: cosine similarity over document centroids, with a time window

Documents are grouped into events by cosine similarity between document-level centroids
(the mean of a document's chunk embeddings), gated by a time window.

- **Similarity threshold: 0.78**
- **Time window: 14 days** between the candidate document's publication time and the
  cluster's most recent member

The threshold started at 0.82 and was lowered on the first real measurement. With
`bge-small`, two notices about the same port closure scored **0.8196**, and an unrelated
grid-maintenance notice scored **0.4881**. A 0.82 cut would have failed to merge the
genuinely related pair — the exact false-negative the clustering exists to prevent — while
anything in roughly 0.55–0.80 separates the two cases comfortably.

That is one pair, not a fitted parameter, and it is recorded here as a data point rather
than a result.

Both are provisional. They are recorded as constants in `gri.processing.cluster` and are
CI-gated inputs: Phase 4's evaluation set includes duplicate and near-duplicate cases
precisely so these can be tuned against labelled data rather than intuition. Changing
either without re-running the evaluation is the kind of silent regression the CI gate
exists to catch.

The time window matters more than it looks. In this domain the *same* chokepoint produces
near-identical notices months apart — "draft restriction at X" recurs seasonally — and
without a window, cosine similarity alone would happily merge this September's
restriction into last February's event.

---

## Consequences

**Positive**

- The whole pipeline, including embeddings and clustering, runs offline and in CI with no
  key and no spend. Only extraction needs the network.
- Cost per document is measured per model, so the cascade can be judged rather than
  assumed.
- Schema validity is structurally guaranteed, which sharpens what the other metrics mean.

**Negative, accepted**

- The embedding model is baked into the image, so the image is larger and a model change
  means a rebuild plus a re-embed.
- 384 dimensions is a real capacity ceiling. It is right for this corpus and would be
  wrong for a broader one.
- The cascade is a second code path, and it needs its own evaluation before it can be
  claimed as a cost win.
- A quote that is real but misused still passes verification. This is disclosed in the
  README rather than papered over.

---

## Revisit triggers

Recorded so that "we should look at this again" is a condition, not a feeling:

| Trigger | Action |
|---|---|
| Escalation rate above ~30% of documents | Drop the cascade; use one model |
| Citation validity below 95% on the labelled set | Investigate before shipping any prompt change |
| Chunk count above ~1M, or ANN recall degrading | Revisit pgvector index strategy (ADR-0001 D3) |
| Non-English sources become material | Revisit the embedding model — `bge-small` is English-only |
| Duplicate detection F1 below target on the labelled set | Re-tune the 0.82 / 14-day constants against data |
