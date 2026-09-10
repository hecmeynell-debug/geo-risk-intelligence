# CONSTRAINTS

This file is the project's controlling document. Where any other document, prompt,
issue, or code comment conflicts with this file, **this file wins**. Every planning
step must re-state the hard non-goals in Section 2 before work begins.

---

## 1. What this project is

`geo-risk-intelligence` is an **auditable, open-source risk intelligence platform** for
**maritime, energy, and supply-chain disruption events**.

It ingests a small number of selected public and official sources, produces structured,
cited, human-reviewable event records, and exposes them through an API and a simple
dashboard.

It is a **data + intelligence engineering project with constrained LLM workflows**.

It is **not** a chatbot, and **not** an operational surveillance product.

### The core discipline

> Every extracted claim must be traceable to source text.
> When evidence is weak, the system abstains or flags for human review.
> **Abstention is always preferred over an unsupported claim.**

---

## 2. Hard non-goals - NEVER violate

These are not preferences. They are constraints. A change that violates any of them is
rejected regardless of how well it is implemented.

| # | Non-goal |
|---|---|
| **NG-1** | **No surveillance framing.** The system is never presented as an authoritative security or intelligence product. Language in the UI, API, README, and briefings must reflect that this is an evidence-aggregation tool over public reporting. |
| **NG-2** | **No non-public, scraped, or restricted sources.** Only sources that are public and whose terms permit the use. No paywalled content, no credentialed access, no circumvention of access controls, no scraping against a site's stated terms or robots.txt. |
| **NG-3** | **No unverified claims presented as fact.** Source attribution is mandatory. A report *claims* something; the system does not assert it independently. |
| **NG-4** | **No individual targeting.** No threat scoring of people, no person-level dossiers, no tracking of named individuals, no military operational analysis, no targeting support of any kind. |
| **NG-5** | **No untraceable free text.** No "assessment" prose that cannot be traced to cited source passages. Every summary sentence must be grounded in verified evidence. |
| **NG-6** | **No global coverage.** Stay inside the maritime / energy / supply-chain disruption niche. Out-of-niche material is classified `background` and dropped. |
| **NG-7** | **No over-engineering.** No Kubernetes, no multi-cloud, no model fine-tuning in v1. Docker Compose and a single Postgres are the target. Add infrastructure only when a concrete, demonstrated need requires it. |

### 2.1 How the non-goals are enforced in code

Non-goals are not left to good intentions. Each has a mechanical enforcement point:

- **NG-2** - a source adapter cannot be enabled until a terms review is recorded in the
  `sources` table (`terms_reviewed_at`, `terms_url`, `terms_note`). Adapters ship
  `enabled = false`. See `docs/source-provenance-policy.md`.
- **NG-3 / NG-5** - every `events` row must have at least one verified row in
  `event_evidence`, where `verified = true` means the exact quote was found in the
  stored source text. Unverified extractions cannot be published.
- **NG-4** - the `entities.entity_type` column carries a `CHECK` constraint whose
  allowed set **excludes `person`**. Person-level actors cannot be stored, so they
  cannot be scored, ranked, or served.
- **NG-4** - `event_locations` describes fixed features (ports, straits, terminals,
  corridors) and administrative areas. There is no schema affordance for a position
  time-series, so movement of an individual entity cannot be recorded.
- **NG-6** - the event-type taxonomy (Section 4) is closed. An extraction that does not
  fit it is `background`, not a new type.

---

## 3. Niche and sources

**In scope:** disruption to maritime shipping, energy production/transport, and physical
supply chains - port and canal disruption, chokepoint transit restrictions, pipeline and
refinery outages, LNG and terminal disruption, grid disruption affecting energy supply,
subsea cable damage, freight and customs delays, and public regulatory or sanctions
notices that bear directly on the above.

**Out of scope:** everything else. Politics, conflict reporting, financial markets, and
cyber incidents are in scope **only** where a source directly reports an effect on
maritime, energy, or supply-chain operations - and then the event is recorded as the
operational disruption, not as the underlying political event.

**Source count:** 5-10 sources. This is a ceiling, not a target. See
`docs/source-provenance-policy.md` for the approval gate and the provenance fields
recorded for every document.

---

## 4. Event taxonomy (closed set)

An extraction that does not fit one of these is `background`.

`port_disruption`, `canal_transit_restriction`, `strait_transit_restriction`,
`vessel_incident`, `piracy_or_armed_robbery_report`, `maritime_security_advisory`,
`pipeline_disruption`, `refinery_outage`, `lng_terminal_disruption`,
`power_grid_disruption`, `energy_export_restriction`, `subsea_cable_damage`,
`labor_action_transport`, `customs_or_border_delay`, `supply_chain_shortage_notice`,
`sanctions_or_regulatory_notice`, `other_in_scope`

---

## 5. Severity - definition

Severity describes **reported operational disruption**, not geopolitical importance and
not harm to people. It is assigned **only** from cited evidence.

| Severity | Definition |
|---|---|
| `informational` | An advisory, warning, or notice with **no confirmed operational impact reported**. |
| `low` | Localized impact at a single facility or route, reported as resolved or expected to resolve within ~24 hours. No throughput reduction reported. |
| `moderate` | Sustained disruption (~1-7 days) at a single node, **or** a measurable but contained reduction in throughput/volume reported in the source. |
| `high` | Corridor-level or multi-node disruption, **or** more than 7 days at a critical node, **or** a source-reported material reduction in regional throughput or supply. |
| `severe` | Chokepoint or corridor closure with source-reported cross-regional supply effects. |

**Assignment rule.** If the source reports no impact information, severity is
`informational`. Severity is **never** inferred from tone, headline prominence, or the
model's background knowledge. `severity_rationale` must cite the evidence that supports
the level.

---

## 6. Confidence and the human-review path

`confidence` is a 0.0-1.0 score for **how well the extracted record is supported by the
cited source text** - not a probability that the event occurred.

| Band | Meaning |
|---|---|
| 0.85 and above | All required fields grounded in verified quotes from a source whose terms are approved. |
| 0.60 to 0.85 | Grounded, but with a partial field, a single-source claim on a material detail, or hedged source language. |
| below 0.60 | Weak, conflicting, or incomplete evidence. |

**`requires_human_review` is set to `true` when any of the following hold:**

1. `confidence` is below 0.60
2. Any citation failed verification (quote not found in stored source text)
3. Sources conflict on a material field (date, location, severity, actor)
4. `severity` is `high` or `severe` (high-impact claims always get a human)
5. The record names an actor the extractor could not resolve to an allowed entity type
6. The item was classified as an **update** to an existing event but the change note
   cannot be grounded in new evidence

Nothing with `requires_human_review = true` appears in a briefing as an established
event. It appears in the review queue.

**Abstention is a valid, tracked outcome.** An extraction returning "insufficient
evidence" is a success, not a failure, and is measured as such in the evaluation harness.

---

## 7. Working agreement

- **One phase at a time.** Human sign-off is required before the next phase starts.
- **Small, reviewable changes.** A human reads every diff before commit.
- **Simplest reliable design.** Trade-offs are documented in `docs/adr/`.
- **Agent count is locked at five** (Planner/Coordinator, Coder, Tester, Reviewer,
  Evaluation/LLMOps). Expansion requires explicit human approval.
- **Prompt and model version are recorded on every extraction.** No exceptions.

---

## 8. Definition of Done

The project is done when all of the following are true:

- [ ] Reproducible from a clean checkout with Docker Compose (one command)
- [ ] Every event record carries source links and verifiable quotes
- [ ] Schema validation and citation validation are enforced **in code and in tests**
- [ ] A human review path exists (approve / reject / edit, with a recorded reason)
- [ ] Evaluation metrics are measured and visible
- [ ] CI is green and **fails** on broken extraction or citation checks
- [ ] Non-goals and limitations are documented and honest about what the system cannot do
- [ ] The project can be explained in an interview as **auditable open-source risk
      intelligence** - not as a chatbot, and not as a surveillance tool

---

## 9. Limitations we state publicly

These are disclosed in the README, not buried:

- Coverage is limited to a handful of sources and is **not** comprehensive.
- Absence of an event in this system is **not** evidence that nothing happened.
- Event records reflect **what sources reported**, including any source error or bias.
- Timeliness is bounded by source publication and a 30-60 minute polling interval.
- Automated extraction makes mistakes; the review queue and confidence score exist
  because of that, and low-confidence records are visibly marked as such.
