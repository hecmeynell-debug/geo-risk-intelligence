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

## Measured on the first live run (2026-09-10)

Two documents through the real API via `scripts/live_extraction_smoke.py`. Small sample,
recorded because it already contradicts one assumption in this ADR.

| Document | Path | In tokens | Out | Latency | Cost |
|---|---|---|---|---|---|
| Clear, well-evidenced notice (458 chars) | Sonnet only | 5,354 | 1,011 | 12.1 s | $0.0208 |
| Thin, hedged notice (228 chars) | Sonnet, then Opus | 5,270 + 5,447 | 146 + 104 | 7.5 s | $0.0418 |

**Behaviour was correct on both.** The clear notice produced an event with 6/6 citations
verified against the source. The thin notice was abstained on by Sonnet *and* by Opus,
each giving an accurate reason — neither invented a record, which is the behaviour the
whole design exists to produce.

Two findings:

**1. The prompt dominates cost, not the document.** A 228-character notice cost 5,270
input tokens, because the instruction block is ~5,000 tokens and the document is
rounding error. At a 30-minute cadence across ten sources this is essentially the entire
bill, and it is almost all identical bytes on every call.

The fix is prompt caching: the instruction block is stable and the document is volatile,
so splitting them at a cache breakpoint (stable content in `system`, document in the user
message) should cut input cost on repeat calls substantially. That restructures the
prompt, so it requires a `v2` — deliberately **not** done as a drive-by, because
`scripts/check_prompts.py` exists precisely to stop released templates changing quietly.

**2. Escalating on abstention may not earn its cost.** In the one case observed, Opus was
spent 2.5x Sonnet's price to *confirm* a correct abstention. If thin sources are common —
and in this domain they are — this specific trigger burns money to change nothing.

That is one observation, not a measurement. Phase 4 should report escalation outcomes
split by trigger (failed citation / low confidence / abstention) rather than in
aggregate, because the three triggers plainly have different value and the aggregate
would hide it. **If the abstention trigger rarely changes the outcome, remove it and keep
the other two.**

---

## Prompt caching, measured (2026-09-10)

The finding above ("the prompt dominates cost") was acted on: `EXTRACT_EVENT_V2` is v1
split at a cache breakpoint. The instruction text is taken verbatim from v1 by slicing
v1's own template, so the two halves concatenate back to v1 byte-for-byte and a cost
comparison is not confounded by a wording change. v1 stays registered, so historical
measurements still refer to a prompt that exists.

Measured on the same synthetic notice, Sonnet 5, input side only (output tokens varied
between runs and would otherwise muddy the comparison):

| | Input tokens | Input cost | vs uncached |
|---|---|---|---|
| v1, uncached | 5,354 | $0.010708 | — |
| v2, cold (cache write) | 251 + 5,104 written | $0.013262 | **+24%** |
| v2, warm (cache read) | 251 + 5,104 read | $0.001523 | **-86%** |

**Caching is not free, and for a single isolated call it is a loss.** A cache write costs
~1.25x the input rate; a read costs ~0.1x. Break-even is **two calls inside one cache
window**:

| Calls in window | Cached | Uncached | |
|---|---|---|---|
| 1 | $0.013262 | $0.010708 | more expensive |
| 2 | $0.014785 | $0.021416 | cheaper |
| 4 | $0.017830 | $0.042832 | much cheaper |

### The caveat that matters for this workload

The default `ephemeral` TTL is **5 minutes**, and the polling cadence is **30-60 minutes**.
So the cache helps *within* a tick — where a feed yields several new documents processed
back to back — and is cold again by the next tick. A tick that yields exactly one new
document pays the write premium and gets nothing back.

Whether caching is a net win therefore depends on documents-per-tick, which is a property
of the sources and is currently unmeasured because no source is enabled. It is very
likely positive (feeds usually deliver in batches), but it is not proven, and it would be
dishonest to quote the 86% figure as the expected saving.

**Not done, deliberately:** a 1-hour cache TTL (`{"type": "ephemeral", "ttl": "1h"}`)
would keep the cache warm across polls and probably suits this cadence far better. It is
not implemented because the 1-hour write multiplier is not in
`PRICING_USD_PER_MTOK`, and shipping a guessed rate would make `cost_usd` a fiction —
the one thing the telemetry exists to prevent. Look the rate up, then decide.

`extractions` now records `cache_write_tokens` and `cache_read_tokens`, so once sources
are enabled the real distribution is measurable rather than argued about.

---

## Revisit triggers

Recorded so that "we should look at this again" is a condition, not a feeling:

| Trigger | Action |
|---|---|
| Escalation rate above ~30% of documents | Drop the cascade; use one model |
| Abstention-triggered escalations rarely change the outcome | Remove that trigger, keep the other two |
| Input cost dominated by the static prompt | Done: prompt v2 splits at a cache breakpoint |
| Documents-per-tick averages below ~2 | Caching is a net loss at that volume; reconsider, or move to a 1h TTL |
| Deciding on a 1h cache TTL | Get the 1h write multiplier from the pricing page first; do not guess it |
| Citation validity below 95% on the labelled set | Investigate before shipping any prompt change |
| Chunk count above ~1M, or ANN recall degrading | Revisit pgvector index strategy (ADR-0001 D3) |
| Non-English sources become material | Revisit the embedding model — `bge-small` is English-only |
| Duplicate detection F1 below target on the labelled set | Re-tune the 0.82 / 14-day constants against data |
