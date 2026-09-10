# ADR-0001: Overall architecture and niche choice

- **Status:** Accepted
- **Date:** 2026-09-10
- **Phase:** 0 (Foundations)
- **Supersedes:** none

---

## Context

We are building an open-source risk intelligence platform that turns public reporting
into structured, cited, human-reviewable event records. Two failure modes dominate this
problem space, and both are more likely than a technical failure:

1. **The credibility failure.** A system that emits confident, ungrounded prose about
   geopolitical events is worse than no system. It looks authoritative precisely where it
   is weakest.
2. **The scope failure.** "Global risk intelligence" is unbounded. Unbounded scope
   produces shallow coverage everywhere and defensible coverage nowhere.

The architecture below is chosen to make both failures hard to commit rather than merely
discouraged.

---

## Decision

### D1. Niche: maritime, energy, and supply-chain disruption

We restrict to physical disruption of shipping, energy, and supply chains.

**Why this niche:**

- It has **official publishers**. Maritime authorities, energy regulators, and canal
  authorities publish structured notices with clear rights status. We are not forced to
  depend on commercial newswires whose terms restrict us.
- Events are **physically anchored** - a port, a strait, a terminal, a pipeline. That
  makes deduplication and clustering tractable, and it keeps locations at the level of
  fixed infrastructure rather than people.
- It has a **real severity definition** grounded in throughput and duration, rather than
  a subjective importance score.
- It stays clear of NG-4 naturally: the actors are ports, operators, authorities, and
  corridors, not individuals.

**Rejected alternative:** general geopolitical risk. It fails all four points above, and
its natural output is exactly the untraceable "assessment" prose that NG-5 forbids.

### D2. Evidence-first data model

The `events` table is a *derived* artefact. The primary record is the raw document with
full provenance, and every event field must be traceable back to a quote in stored source
text through the `event_evidence` table.

Consequences, accepted deliberately:

- An event cannot be published without at least one **verified** citation, where verified
  means the exact quote string was located in the stored `clean_text`.
- Summaries are grounded assemblies of cited claims, not free composition.
- The system can say "insufficient evidence", and that outcome is measured as a success.

This is the mechanical form of NG-3 and NG-5. It is enforced in the schema, in the
validation code, and in tests - not in a prompt instruction, because prompt instructions
are not enforcement.

### D3. Postgres + pgvector as the single datastore

One Postgres instance holds relational data and embeddings.

**Why:** the corpus is small (a handful of sources at a 30-60 minute cadence - thousands
of documents, not billions). pgvector handles this comfortably. A dedicated vector
database would add an operational component, a second consistency domain, and a sync
problem, to solve a scale problem we do not have. NG-7.

**Revisit when:** chunk count exceeds roughly 1M and ANN recall or latency measurably
degrades. Recorded as a trigger, not a plan.

### D4. Docker Compose, no orchestrator

Services: `db` (Postgres + pgvector), `api` (FastAPI), `worker` (scheduled ingestion and
processing). A frontend is added in Phase 3.

**Why:** the Definition of Done requires reproducibility from a clean checkout in one
command. Compose achieves that. Kubernetes is explicitly excluded by NG-7.

**Redis is deliberately absent.** The worker uses an in-process scheduler and Postgres
row locks (`SELECT ... FOR UPDATE SKIP LOCKED`) for work claiming. That is sufficient for
a single worker at this cadence. Redis is added only if we demonstrate a concrete need -
multiple workers contending, or a queue depth Postgres cannot absorb.

### D5. Schema-constrained extraction with post-hoc citation verification

The LLM is used for one job: turning a document into a candidate structured record. It is
never the final authority on correctness.

The pipeline is:

```
document -> LLM extraction -> Pydantic schema validation -> citation verification
         -> confidence scoring -> publish OR route to human review
```

**Citation verification is deterministic code, not a model call.** Each quote is located
in the stored `clean_text` by normalised string match. A quote that cannot be located
fails, and the record is flagged. This is the single most important design decision in
the project: it converts "the model claims it cited this" into a checkable fact.

We accept that this catches fabricated quotes but not misread ones - a real quote can
still be attached to a wrong conclusion. That residual risk is what the confidence score,
the `high`/`severe` review trigger, and the human queue exist to absorb. We state this
limitation publicly rather than implying the check is stronger than it is.

### D6. Prompt and model versioning on every extraction

Every row in `extractions` records `prompt_id`, `prompt_version`, `model_name`, and
`model_version`, alongside token counts, cost, and latency.

**Why:** without this, an evaluation number is not attributable to anything and a
regression cannot be localised. It also makes cost per document a measured quantity
rather than an estimate.

### D7. Change detection as a first-class field

Incoming items are classified as `new_event`, `update`, `duplicate`, or `background`. An
`update` must carry a `change_note` grounded in new evidence.

**Why:** in this domain the same incident is re-reported many times. A system that
creates a new event per article is not usable. "What is new since the last update" is the
question an analyst actually asks.

---

## Architecture

```
Public / official sources  (5-10, each terms-reviewed and individually enabled)
        |
        v
Ingestion service (scheduled, 30-60 min, conditional requests, backoff)
        |
        v
Raw document store  -- url, source, published_at, retrieved_at,
                       retrieval_method, raw_hash, content_hash, clean_text
        |
        v
Processing:  clean -> deduplicate -> chunk -> embed -> cluster
                   -> LLM extract -> schema validate -> verify citations
                   -> score confidence -> flag for review
        |
        v
PostgreSQL + pgvector
   sources, ingestion_runs, raw_documents, document_chunks,
   event_clusters, events, entities, event_* link tables, event_evidence,
   prompts, extractions, review_decisions,
   evaluation_datasets, evaluation_examples, evaluation_runs, evaluation_results
        |
        v
FastAPI  --  /events  /events/{id}  /search  /briefing  /review  /health
        |
        v
Simple dashboard  --  timeline, filters, event detail with evidence panel,
                      aggregate location view (fixed infrastructure only)
```

---

## Consequences

**Positive**

- Every claim is auditable to a quote in stored text.
- Non-goals are enforced by schema constraints and code paths, not by convention.
- The stack runs on a laptop and is reproducible in one command.
- Evaluation is attributable to a specific prompt and model version.

**Negative, accepted**

- Coverage is narrow by construction. This is the point, and the README says so.
- Deterministic quote matching is brittle against source text that is edited after
  publication. We store `clean_text` at retrieval time, so verification runs against what
  we actually saw; a later edit at the publisher does not silently invalidate history,
  but it does mean our stored text can diverge from the live page.
- Requiring verified citations means some genuine events are held in review rather than
  published. We prefer that direction of error.
- A single Postgres is a single point of failure. Acceptable for a portfolio and local
  deployment; it would not be acceptable for an operational system, and we say so.

---

## Open questions for later phases

- **Embedding model and dimension** (Phase 2). The `document_chunks.embedding` column is
  provisionally `vector(1536)`. `embedding_model` and `embedding_dim` are stored per row
  so a model change is detectable rather than silent. Changing dimension requires a
  migration and a re-embed; ADR-0002 will record the choice.
- **Clustering thresholds** (Phase 2) - similarity cutoff and time window for grouping
  documents into one event. To be chosen against the labelled set, not by intuition, and
  recorded in an ADR because CI gates on it.
- **Extraction model choice** (Phase 2) - to be decided on measured schema-valid rate,
  citation validity, and cost per document.
