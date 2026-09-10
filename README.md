# geo-risk-intelligence

**Auditable open-source risk intelligence for maritime, energy, and supply-chain
disruption.**

This system ingests a small number of public and official sources, and turns them into
structured event records where **every claim is traceable to a quote verified against the
source text we retrieved**. Records with weak, conflicting, or incomplete evidence are
routed to a human review queue rather than published as established.

> **Status: Phase 0 (Foundations).** The stack, schema, and constraints are in place. No
> sources are ingested yet and there are no events in the database. See
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
FastAPI  ──  /events  /events/{id}  /search  /briefing  /review  /health
        │
        ▼
Dashboard  ──  timeline, filters, event detail with evidence panel,
               aggregate location view (fixed infrastructure only)
```

Three services in Docker Compose: `db`, `api`, `worker`, plus a one-shot `migrate` job
that both long-running services wait on. No Kubernetes, no Redis, no second datastore.
The reasoning is in [ADR-0001](docs/adr/ADR-0001-architecture-and-niche.md).

---

## The core idea: citations that are actually checked

The LLM has exactly one job — turn a document into a *candidate* structured record. It is
never the final authority on correctness.

```
document → LLM extraction → Pydantic schema validation → citation verification
         → confidence scoring → publish OR route to human review
```

**Citation verification is deterministic code, not another model call.** Each quote is
located in the stored `clean_text` by normalised string match. A quote that cannot be
found fails, and the record is flagged. That converts "the model says it cited this" into
a checkable fact.

What this catches: fabricated quotes. What it does **not** catch: a real quote attached to
a wrong conclusion. That residual risk is what the confidence score, the mandatory review
of `high`/`severe` records, and the human queue exist to absorb. We would rather say this
plainly than imply the check is stronger than it is.

---

## Non-goals, and how they are enforced

The full list is in [CONSTRAINTS.md](CONSTRAINTS.md). Each one has a mechanical
enforcement point rather than only a policy statement:

| Non-goal | Enforcement |
|---|---|
| No non-public or restricted sources | A source cannot be `enabled`, and its full text cannot be stored, unless `terms_reviewed_at` is set — a database `CHECK`, plus a CI audit |
| No individual targeting | `entities.entity_type` has **no `person` member**; a `CHECK` constraint rejects it, so person-level records are unrepresentable |
| No unverified claims as fact | An event cannot be published from an extraction unless `schema_valid AND citation_valid` — a database `CHECK` |
| No untraceable assessment | Every event needs verified rows in `event_evidence`; `verified = true` requires a recorded verification method and timestamp |
| No unbounded scope | The event taxonomy is a closed `CHECK`ed set; anything else is `background` |
| No aggressive polling | `poll_interval_seconds BETWEEN 1800 AND 3600`, in both config validation and the database |
| High-impact claims are always reviewed | `severity NOT IN ('high','severe') OR requires_human_review` — a database `CHECK` |

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
  api/               FastAPI app (health only in Phase 0)
  worker/            scheduled worker (heartbeat in Phase 0)
  ingestion/         source adapters                          (Phase 1)
  processing/        chunk, embed, cluster, extract, verify   (Phase 2)
migrations/          Alembic
scripts/             CI guards: source terms audit, migration drift check
docs/adr/            architecture decision records
tests/               unit tests, plus live-database constraint tests
```

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundations: Compose stack, schema, migrations, constraints, CI | **Complete** |
| 1 | Ingestion and raw store: 5–10 adapters, provenance, idempotency | Not started |
| 2 | Core pipeline: chunk, embed, cluster, extract, verify citations, API | Not started |
| 3 | Product interface: feed, filters, evidence panel, briefing, review queue | Not started |
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

## Licence

Apache-2.0.
