# ADR-0005: Automatic processing, and the guards on what it spends

- **Status:** Proposed
- **Date:** 2026-09-11
- **Phase:** closes a gap between Phase 1 (scheduled worker) and Phase 2 (pipeline)
- **Builds on:** [ADR-0001](ADR-0001-architecture-and-niche.md) D4, [ADR-0002](ADR-0002-embeddings-and-extraction.md) D2

---

## Context

ADR-0001's architecture runs ingestion → raw document store → processing. Phases 1 and
2 built both halves, but **nothing connected them**: the worker's tick only ingested, and
nothing outside the tests ever called `process_document`. Once a source was enabled,
documents would have accumulated in the raw store and never become events.

Wiring it in is simple. The reason it needs an ADR is that it makes the system **spend
real money automatically**, with no human in the loop, as soon as a source is enabled.

## Decision

Each worker tick, after ingestion, extracts events from documents that are pending — in
its own session, committing after each document.

### Pending means

A document with stored `clean_text`, not tombstoned, with no extraction that reached a
terminal outcome. `abstained`, `citation_invalid`, and `schema_invalid` are terminal:
they are answers, not failures to retry. `error` is not terminal, but a document is
retried at most `MAX_ATTEMPTS` (3) times, then left alone.

Metadata-only documents (the source's terms forbid storing the body) are never
processed: with no stored text there can be no verified quote, so extraction would buy
nothing.

### The guards

| Guard | Default | Setting |
|---|---|---|
| Documents extracted per tick | 20 | `GRI_MAX_EXTRACTIONS_PER_TICK` |
| Recorded extraction spend per UTC day | $1.00 | `GRI_DAILY_EXTRACTION_BUDGET_USD` |

Setting either to 0 turns automatic processing off.

**The budget is measured from the `cost_usd` recorded on every extraction** — the same
telemetry the evaluation relies on — not estimated. It is checked before *each* document,
so a tick stops partway once the line is crossed instead of finishing its batch first.

The $1.00 default is deliberately low: at the ~$0.02 per document measured so far it
allows roughly 50 documents a day, which comfortably covers a handful of sources at a
30-minute cadence while making an accident cheap. Raise it once real volumes are known.

### One transaction per document

The worker commits after every document. A single transaction per tick would be simpler,
but a crash on document 19 would roll back the 18 already processed — and they would be
extracted, and paid for, again on the next tick.

### No credential, no attempt

If no model credential is configured, processing is skipped. The worker warns only when
documents are actually waiting, so a checkout without a key does not log a warning every
60 seconds forever.

## Consequences

- Enabling a source now produces events without anyone running a script. That is the
  point, and it is why the guards exist.
- **Worth knowing before enabling the first source:** with `GRI_ANTHROPIC_API_KEY` set,
  the worker will process every pending document up to the daily budget. The budget is
  the only thing standing between a misconfigured source and a surprise bill; set it
  deliberately.
- A document that errors three times stays unprocessed and needs a human to look at why.
  Its extraction rows record the errors.

## Verified

One real worker tick over a synthetic unprocessed document: it was picked up, embedded
with `bge-small`, extracted with Sonnet 5 ($0.021), committed, and the day's measured spend
rose by exactly the recorded cost. 18 tests cover the pending rules, the per-tick cap, the
budget (including stopping mid-tick), retry limits, idempotency across ticks, and failure
isolation.
