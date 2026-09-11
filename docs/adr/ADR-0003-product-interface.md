# ADR-0003: Product interface — dashboard, briefing, and the review path

- **Status:** Proposed — D4 needs a human decision before this is accepted
- **Date:** 2026-09-11
- **Phase:** 3 (Product Interface)
- **Builds on:** [ADR-0001](ADR-0001-architecture-and-niche.md) D4, [ADR-0002](ADR-0002-embeddings-and-extraction.md)

---

## Context

Phase 2 produces verified, cited event records. Phase 3 makes them inspectable: a feed,
an event page with its evidence, an aggregate location view, a daily briefing, and a
queue where a human approves, rejects, or edits flagged records.

Every one of those surfaces is a place the project's promises could quietly break. An
event page could render a record without its quotes. A briefing could grow a sentence of
generated analysis. A flagged record could appear in the feed looking established. The
decisions below are mostly about making each surface enforce the promise it could most
easily break.

---

## Decisions

### D1. Server-rendered HTML, no JavaScript framework

The dashboard at `/ui` is Jinja2 templates rendered by FastAPI. No React, no bundler, no
npm, no build step.

**Why:** the Definition of Done requires one-command reproducibility, and NG-7 forbids
over-engineering. A frontend toolchain would add a second build, a second dependency
tree, and a second thing that can break `docker compose up`. The dashboard's job is to
show records and their evidence clearly; it needs no client-side state to do that.

**One code path for evidence.** Every page renders the API's own response objects —
`build_event_detail`, `list_events`, `pending_queue`, `build_briefing` — rather than
querying the database its own way. "Never serve an event without its evidence" and
"never present a flagged record as established" therefore each have one implementation,
shared by the JSON API and the HTML, instead of two that could drift.

**Untrusted text.** Every quote and source URL came from an external publisher. Jinja2
autoescapes all output, and a `safe_url` filter renders a link only if it is `http` or
`https` — a `javascript:` URL in a hostile feed would otherwise execute when a reviewer
clicked "source document". Both are tested.

### D2. The briefing is assembled, never generated

`gri.briefing.build_briefing` builds the daily briefing from established records and
their **verified** quotes. It makes no model call. A test inspects the module source and
fails if the extraction provider is ever imported into the briefing path, and another
asserts the same day briefs byte-identically twice.

**Why:** the briefing is the most quotable thing the system produces, which makes it the
easiest place to break NG-3 and NG-5. A model-written summary paragraph would read well
and be untraceable — exactly the "assessment" prose the constraints forbid.

**What it leaves out is reported.** Records awaiting review are withheld, but *counted*,
so a partial briefing is visibly partial. Records with no verified quote are dropped and
counted too (this should never happen; it is reported because a briefing line without
evidence must never be served).

### D3. The aggregate location view is a table, not a map

`/locations` returns counts of established records per fixed feature and per country. No
coordinates, no per-event positions, nothing ordered in time.

**Why no map:** the pipeline does not geocode — `event_locations.latitude` and
`longitude` are never set. Drawing a map would mean adding a geocoder (a new external
dependency) and plotting points the evidence never supplied, implying a precision the
records do not carry. The counts convey the aggregate picture without either cost. NG-4
is served by construction: the view answers "where has disruption been reported", never
"where is something now".

If a map is wanted later, it should plot fixed features from a static, offline gazetteer
of ports and chokepoints — never coordinates extracted from reporting.

### D4. What a human approval can change — **open, needs a decision**

**The finding.** Every trigger that holds a *published* record in review is also
enforced by a database CHECK:

| Trigger | Enforced by |
|---|---|
| `high` / `severe` severity | `ck_events_high_severity_requires_review` |
| confidence below 0.60 (including the metadata-only cap of 0.50) | `ck_events_low_confidence_requires_review` |

A failed citation never publishes a record at all, and conflicting-sources detection is
not implemented yet. So **in the current system, "approve" can never clear a record's
review flag**: every flagged record the pipeline can produce is flagged by a rule the
database insists on. An approved record therefore never reaches the feed, the location
counts, or a briefing. The only decision that can clear the flag is an *edit* that lowers
severity on a record with confidence of at least 0.60.

That is not a bug in the code — it is exactly what CONSTRAINTS.md Section 6 says:
"Nothing with `requires_human_review = true` appears in a briefing as an established
event." But it has a consequence that is easy to miss: **the most important records —
`high` and `severe` — are precisely the ones that can never appear in a briefing**, even
after a human has checked every quote.

**What this PR does:** implements the rule as written. Approval is recorded, and the
response, the review page, and the briefing all *say* that the flag was kept and why.
The briefing counts approved-but-flagged records separately from those still awaiting a
decision, so the consequence is visible in the product rather than silent.

**The options, for a human to choose between:**

| Option | Change | Consequence |
|---|---|---|
| **A. Keep as is** (this PR) | None | `high`/`severe` records are only ever visible on their event page and in the review queue. Briefings systematically omit the most significant disruption. |
| **B. Treat approved records as established** | Feed, locations and briefing include `review_status IN ('approved','edited')` regardless of the flag. Amend CONSTRAINTS.md §6. | The flag keeps meaning "a human had to look". Approved severe records appear, visibly marked as human-approved. The database CHECKs stay as they are. |
| **C. Let approval clear the flag** | Relax both CHECKs to `... OR review_status IN ('approved','edited')`. Migration. Amend CONSTRAINTS.md §6. | Simplest downstream, but the database no longer guarantees that a severe record was ever seen by a human *unless* it says so in `review_status` — the guarantee moves from one column to two. |

**Recommendation:** B. It keeps both database guarantees intact, keeps the meaning of the
flag honest ("this needed a human"), and changes only what the read paths consider
established — a small, testable change. But it amends the controlling document, so it is
not made here.

### D5. The summary is not editable

A reviewer may edit `title`, `event_type`, `event_date`, and `severity`. Not `summary`.

**Why:** the summary is assembled from cited evidence. A reviewer who rewrites it produces
prose no source supports — NG-5, just with a human author instead of a model. A reviewer
who thinks the summary is wrong should reject the record. When severity is edited, the
model's `severity_rationale` is replaced with one naming the reviewer and their reason,
because the old rationale argued for the old level.

### D6. Every decision is validated in full before anything is applied

`apply_decision` checks the reason, the reviewer, the decision, and every edit before it
touches the record. A single pass that validated and assigned together would leave
earlier fields written when a later one was rejected — a half-applied edit with no
decision row behind it, which is unauditable. This defect was found and fixed in the
first (cloud-built) implementation of this phase; it is built in from the start here and
covered by a regression test.

---

## Limitations stated plainly

- **No authentication.** The reviewer name on a decision is self-declared. The review
  endpoints and pages must not be exposed beyond a trusted network. Adding auth is a
  real piece of work and is not justified for a local tool (NG-7), but it would be a
  prerequisite for any shared deployment.
- **No CSRF protection** on the review form, for the same reason: without authentication
  there is no session to protect. The same prerequisite applies.
- **The briefing's day is UTC** and is keyed on when a record was first seen or last
  updated, not on `event_date`. A notice published today about a closure next week
  appears in today's briefing — which is what "what was reported today" means.

---

## Consequences

**Positive**

- A user can inspect every record, see each quote and whether it verified, follow a link
  to the source, and generate a briefing in which every line is a field or a quote.
- The two promises most at risk in a UI — evidence always shown, flagged records never
  presented as established — each have one implementation, shared with the API.
- No new infrastructure: two small Python dependencies (Jinja2, python-multipart).

**Negative, accepted**

- Until D4 is decided, briefings omit `high`/`severe` records entirely. The briefing says
  so, but it is still a significant gap in the product.
- No map. The counts are adequate for the aggregate picture; a map would need an offline
  gazetteer to be done honestly.
