# geo-risk-intelligence

**Auditable open-source risk intelligence for maritime, energy, and supply-chain
disruption.**

This system ingests a small number of public and official sources, and turns them into
structured event records where **every claim is traceable to a quote verified against the
source text we retrieved**. Records with weak, conflicting, or incomplete evidence are
routed to a human review queue rather than published as established.

> **Status: Phase 3 (Product Interface) in review.** Phases 0–2 are complete: the stack,
> schema, adapters, raw document store, and the full pipeline — chunk, embed, cluster,
> extract against a strict schema, verify every citation, score confidence, and publish
> or route to review. Phase 3 adds the dashboard, the daily briefing, the location view,
> and the human review queue. **No source is enabled**, so nothing is
> being fetched and the database holds no documents — enabling a source requires a human
> to record a terms review first. See [Sources](#sources-nothing-is-enabled) and
> [Roadmap](#roadmap).

---

## What this is, and what it is not

**It is** a data and intelligence engineering project: scheduled ingestion with full
provenance, deduplication and event clustering, schema-constrained LLM extraction,
deterministic citation verification, a human review path, and a measured evaluation
harness.

**It is not** a chatbot, and **not** a surveillance product. It does not track or score
individuals — that is enforced by the database schema, not by policy alone (see
[Non-goals](#non-goals-and-how-they-are-enforced)).

### Stated limitations

- Coverage is limited to a handful of sources and is **not comprehensive**.
- **Absence of an event here is not evidence that nothing happened.**
- Records reflect **what sources reported**, including any source error or bias.
- Timeliness is bounded by source publication and a 30–60 minute polling interval.
- Automated extraction makes mistakes. The confidence score and review queue exist
  because of that, and low-confidence records are visibly marked.

---

## Quickstart

Requires Docker and Docker Compose.

```bash
git clone <this repo>
cd geo-risk-intelligence
docker compose up --build --wait
```

That builds the images, starts Postgres with pgvector, applies migrations, and starts the
API and worker.

| | |
|---|---|
| Dashboard | <http://localhost:8000/ui> |
| API | <http://localhost:8000> |
| OpenAPI docs | <http://localhost:8000/docs> |
| Health | <http://localhost:8000/health> |

`/health` returns 503 unless the database is reachable, pgvector is installed, **and**
migrations have been applied — so a half-built stack fails loudly instead of quietly
serving nothing.

```bash
make help     # all targets
make test     # full suite, including live-database constraint tests
make check    # lint, types, and the tests that need no database
make reset    # tear down and destroy the database volume
```

---

## Architecture

```
Public / official sources  (5–10, each terms-reviewed and individually enabled)
        │
        ▼
Ingestion service (scheduled, 30–60 min, conditional requests, backoff)
        │
        ▼
Raw document store  ── url, source, published_at, retrieved_at,
                       retrieval_method, raw_hash, content_hash, clean_text
        │
        ▼
Processing:  clean → deduplicate → chunk → embed → cluster
                   → LLM extract → schema validate → verify citations
                   → score confidence → flag for review
        │
        ▼
PostgreSQL + pgvector
        │
        ▼
FastAPI  ──  /events  /events/{id}  /search  /briefing  /locations
             /review  /review/{id}/decisions  /health
        │
        ▼
Dashboard (/ui)  ──  feed with filters, event detail with evidence panel,
                     aggregate location counts (fixed features only),
                     daily briefing, review queue
```

Three services in Docker Compose: `db`, `api`, `worker`, plus a one-shot `migrate` job
that both long-running services wait on. No Kubernetes, no Redis, no second datastore.
The reasoning is in [ADR-0001](docs/adr/ADR-0001-architecture-and-niche.md).

---

## The dashboard, and what it refuses to do

Server-rendered HTML from FastAPI at `/ui`. **No React, no bundler, no npm, no build
step** — the templates ship inside the Python package, so `docker compose up --build`
stays one command.

| Page | What it shows | What it refuses |
|---|---|---|
| Feed | Established records, filterable by type, severity, country, date | Records awaiting review — counted and linked, never listed as established |
| Event | The record, and every quote with its verification method and a link to its source | A record without its evidence |
| Locations | Counts per fixed feature and per country | Coordinates, positions, or anything over time (NG-4). No map: the pipeline does not geocode, and plotting points it never measured would imply false precision |
| Briefing | Records first seen or updated that day, each with verified quotes | Any generated prose. There is **no model call** in the briefing path, and CI checks that. What it withholds, it counts |
| Review | Flagged records, why each is flagged, and a decision form | A decision without a reason; an edit to the summary; an approval that pretends to clear a flag the database requires |

Every page renders the API's own response objects, so "never serve an event without its
evidence" has one code path rather than two.

Source text is untrusted: quotes are autoescaped, and a source link is rendered only if
it is `http`/`https` — a `javascript:` URL in a hostile feed is shown as "source link
unavailable" instead.

> **Open decision (ADR-0003 D4).** In the current system, approving a record can never
> clear its review flag: every trigger that holds a published record in review is also a
> database `CHECK`. So `high` and `severe` records — the most important ones — never
> reach a briefing, even after a human has checked every quote. The briefing counts them
> so the gap is visible. [ADR-0003](docs/adr/ADR-0003-product-interface.md) sets out the
> options; changing it means amending CONSTRAINTS.md.

**There is no authentication.** Reviewer names are self-declared. Do not expose the
review pages beyond a trusted network.

## The core idea: citations that are actually checked

The LLM has exactly one job — turn a document into a *candidate* structured record. It is
never the final authority on correctness.

```
document → LLM extraction → Pydantic schema validation → citation verification
         → confidence scoring → publish OR route to human review
```

**Citation verification is deterministic code, not another model call.** Each quote is
located in the stored `clean_text` by normalised string match, using the same
normalisation that produced it — so a quote differing only in whitespace or Unicode form
still verifies, while a quote with different *words* does not. That converts "the model
says it cited this" into a checkable fact.

An extraction whose quotes do not verify **never reaches the `events` table**. That is
enforced in the pipeline, asserted by `tests/test_pipeline_integration.py::TestCitationGate`,
and backed by a database `CHECK` requiring `schema_valid AND citation_valid` before an
extraction can carry an `event_id`.

What this catches: fabricated quotes. What it does **not** catch: a real quote attached to
a wrong conclusion. That residual risk is what the confidence score, the mandatory review
of `high`/`severe` records, and the human queue exist to absorb. We would rather say this
plainly than imply the check is stronger than it is.

---

## Models and cost

| Stage | Model | Where it runs |
|---|---|---|
| Embeddings | `BAAI/bge-small-en-v1.5` (384 dims) | Locally, in the worker, via onnxruntime |
| Extraction | `claude-sonnet-5` | Anthropic API |
| Escalation | `claude-opus-5` | Anthropic API, only when the first pass is weak |

Embeddings run locally so the evaluation harness stays runnable in CI on every push,
with no key and no spend. Extraction escalates to the stronger model only on a failed
citation, a low-confidence result, or an in-niche abstention — so the capability is spent
on the cases where judgement actually matters.

Every model call records its model, prompt version, token counts, latency, and computed
cost as its own `extractions` row, so cost per document is measured rather than estimated.
**Both halves of a cascade are recorded separately**, which is what lets Phase 4 report
quality per model instead of hiding it in an aggregate. The reasoning, and the conditions under
which the cascade should be dropped, are in
[ADR-0002](docs/adr/ADR-0002-embeddings-and-extraction.md).

The whole test suite runs against a scripted extraction provider: **no key, no network,
no spend.** Set `GRI_ANTHROPIC_API_KEY` in `.env` to make real calls.

## Sources: nothing is enabled

Ten candidate sources are registered — maritime authorities, canal authorities, energy
regulators, and public sanctions lists. **All ten are disabled, unreviewed, and
unverified.** Nobody has read any publisher's terms yet, and this repository makes no
claim that any of them permits our use.

Enabling one requires three separate things to be true, each enforced by a database
`CHECK`:

| Gate | Meaning |
|---|---|
| terms review recorded | A human read the terms and wrote down what they say |
| `endpoint_verified` | A human confirmed the feed URL is right and exists |
| `format_confirmed` | A human looked at a real response and confirmed the parse |

```bash
make sources                # what is registered, and its state
make seed-sources           # register the candidates (all disabled)
```

The third gate exists because the adapter configuration in
`src/gri/ingestion/registry.py` is a **starting hypothesis**, written from each
publisher's documented shape rather than from an observed response. Ten hand-written
parsers for feeds nobody has looked at would be ten guesses wearing a lab coat. Instead
there are three real, tested adapter engines — RSS/Atom, JSON API, HTML notices — and the
per-source field mapping is one reviewable block of configuration that a human corrects
during terms review.

A block is treated as an answer, not an obstacle: a `401`, `403`, `451`, or a
robots.txt disallow disables the adapter and flags the source for re-review. It is never
retried and the request is never varied to get around it.

## Non-goals, and how they are enforced

The full list is in [CONSTRAINTS.md](CONSTRAINTS.md). Each one has a mechanical
enforcement point rather than only a policy statement:

| Non-goal | Enforcement |
|---|---|
| No non-public or restricted sources | A source cannot be `enabled` unless `terms_reviewed_at` is set **and** its endpoint and parse are verified — database `CHECK`s, plus a CI audit |
| A block is an answer | `401`/`403`/`451` or a robots.txt disallow raises `SourceBlocked`, which disables the source. No retry, no varied request |
| No individual targeting | `entities.entity_type` has **no `person` member**; a `CHECK` constraint rejects it, so person-level records are unrepresentable |
| No unverified claims as fact | An event cannot be published from an extraction unless `schema_valid AND citation_valid` — a database `CHECK` |
| No untraceable assessment | Every event needs verified rows in `event_evidence`; `verified = true` requires a recorded verification method and timestamp |
| No unbounded scope | The event taxonomy is a closed `CHECK`ed set; anything else is `background` |
| No aggressive polling | `poll_interval_seconds BETWEEN 1800 AND 3600`, in both config validation and the database |
| High-impact claims are always reviewed | `severity NOT IN ('high','severe') OR requires_human_review` — a database `CHECK`, which an approval cannot override |
| No untraceable assessment prose | The briefing is assembled from fields and verified quotes with no model call; a test fails if the extraction provider is imported into that path. Reviewers cannot edit the summary |
| A decision must be auditable | `review_decisions.reason` is `NOT NULL`; blank or whitespace reasons are refused before the database sees them |

These are covered by tests in `tests/test_schema_integration.py`, which assert that
**Postgres itself** refuses the write.

---

## Repository layout

```
src/gri/
  taxonomy.py        closed vocabularies; the single source of truth for CHECK constraints
  config.py          settings, with the polling range enforced at load time
  db.py              engine, session scope, health probe
  models/            SQLAlchemy models -- sources, documents, events, evidence, llmops, eval
  api/events.py      feed, filters, detail, search
  api/briefing.py    the daily briefing endpoint
  api/locations.py   aggregate location counts
  api/review.py      the review queue and decisions
  api/ui.py          the server-rendered dashboard, and its templates/
  briefing.py        assembling a briefing from verified evidence -- no model call
  review.py          applying a human decision, and what a decision may not change
  worker/            scheduled polling of enabled sources
  ingestion/         http, adapters, registry, normalise, store, runner
  processing/        chunk, embed, cluster, extract, verify   (Phase 2)
migrations/          Alembic
scripts/             source registry seeding, terms review CLI, CI guards
docs/adr/            architecture decision records
tests/               unit tests, plus live-database constraint tests
```

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundations: Compose stack, schema, migrations, constraints, CI | **Complete** |
| 1 | Ingestion and raw store: adapters, provenance, idempotency | **Complete** |
| 2 | Core pipeline: chunk, embed, cluster, extract, verify citations, API | **Complete** |
| 3 | Product interface: feed, filters, evidence panel, briefing, review queue | **In review** |
| 4 | Evaluation and operations: labelled set, CI metrics gate, logging | Not started |
| 5 | Polish and packaging | Not started |

Each phase ends at a gate that requires human sign-off before the next begins.

---

## Documentation

- [CONSTRAINTS.md](CONSTRAINTS.md) — the controlling document: non-goals, severity and
  confidence definitions, review triggers, Definition of Done
- [docs/source-provenance-policy.md](docs/source-provenance-policy.md) — source
  eligibility, the terms-review gate, provenance fields, fetching conduct
- [docs/adr/ADR-0001-architecture-and-niche.md](docs/adr/ADR-0001-architecture-and-niche.md)
  — architecture and niche, with the trade-offs
- [docs/adr/ADR-0002-embeddings-and-extraction.md](docs/adr/ADR-0002-embeddings-and-extraction.md)
  — embedding and extraction models, the verification boundary, measured costs
- [docs/adr/ADR-0003-product-interface.md](docs/adr/ADR-0003-product-interface.md)
  — the dashboard, the briefing, the review path, and the open approval question

## Licence

Apache-2.0.
