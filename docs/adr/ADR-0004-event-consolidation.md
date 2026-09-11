# ADR-0004: Event consolidation — duplicates, updates, and conflicts

- **Status:** Proposed
- **Date:** 2026-09-11
- **Phase:** 2 (completes core capability #3), found while reviewing Phase 3
- **Builds on:** [ADR-0001](ADR-0001-architecture-and-niche.md) D7, [ADR-0002](ADR-0002-embeddings-and-extraction.md) D5

---

## Context

The MVP's core capability #3 is to "detect whether an incoming item is a new event, an
update to an existing event, a duplicate, or background". ADR-0001 D7 adds that "an
`update` must carry a `change_note` grounded in new evidence."

Phase 2 built half of this. Documents were clustered correctly, but `process_document`
**created a new event for every verified document regardless of the cluster match**. The
only effect of a match was a canned note, *"Update: new document matched an existing
event."* Two consequences:

1. Two reports of the same port closure became **two events** in the feed and the
   briefing — exactly the unusable one-event-per-article behaviour D7 warned against.
2. The canned note was prose with no evidence behind it, which is what NG-5 forbids.

Neither was caught because every pipeline test processed documents with different text,
and the fake embedder gives different text unrelated vectors.

---

## Decision

When a verified document clusters with an existing active event, the pipeline compares
the new extraction to the record (`gri.processing.consolidate.compare`, a pure function)
and folds the document in instead of creating an event.

### D1. Three outcomes

| Outcome | When | What happens |
|---|---|---|
| **Duplicate** | Nothing material differs | The new document's verified quotes are attached as corroboration. The record, its change note, and its review state are untouched. |
| **Update** | Severity changed, or the new source names places or organisations the record lacks | Grounded changes are applied; a change note is assembled from the quotes. |
| **Conflict** (an update that changes nothing) | Event type or event date contradicts the record | The record is left as it is and goes to a human (trigger 3). |

### D2. Nothing changes without a verified quote from the new document

- A **severity** change is applied only if the new document has a verified quote labelled
  as supporting severity. Otherwise it is not applied, the record goes to review
  (CONSTRAINTS.md §6 trigger 6), and the note says the change had no support.
- A **location or organisation** is added only if the new source's own verified text
  *names* it. The field label a model attaches to a quote is not evidence; the words are.
  A name the text never mentions is silently not added.
- **Sectors** are coarse tags that never appear verbatim; they are added only when a
  quote is labelled as supporting sectors.
- The **title and summary** are never rewritten by an update. They were assembled from
  the first report's evidence; the change note carries what is new.

### D3. The change note is assembled, not written

Each clause is a field change plus the verbatim quote supporting it, e.g.:

> Update from Example Authority published 2026-09-11: severity revised from moderate to
> high: "the Exampleton Port Authority now expects the closure to last at least eight
> days"; location reported: Exampleton Roads: "Around 25 vessels are waiting at the
> Exampleton Roads anchorage."

No sentence comes from model prose, so the note is as traceable as the evidence (NG-5).
It replaces the previous note: its meaning is "what is new since the last update". The
full history remains in `event_documents` (each document's relation) and `event_evidence`.

### D4. Review state after an update

- Conflicts and unsupported changes always send the record to review.
- The database rules still apply: `high`/`severe` severity or confidence below 0.60 keeps
  the flag set.
- **New content on a record a human already cleared (`approved` or `edited`) sends it
  back to `pending`.** An approval covers the content the reviewer saw, not what arrives
  afterwards.
- A record still awaiting review stays in review.
- A **rejected** record receiving an update goes back to `pending` — new evidence merits
  a second look — but stays out of the feed and briefings, because the flag stays set.
  A *duplicate* of a rejected record changes nothing: the same facts were already judged.
- Confidence after an update is the *lower* of the record's and the new extraction's.
  Evidence can disprove optimism; it cannot corroborate it (ADR-0002, `confidence.py`).
  A duplicate leaves confidence unchanged.

### D5. Which event a document merges into

The cluster's most recent active event. Clusters built before this change may hold
several events; merging into the newest keeps each disruption on one live record going
forward. Existing duplicates are not retroactively merged — there is no production data
yet, so there is nothing to repair.

---

## An interpretation to confirm

CONSTRAINTS.md §6 trigger 3 reads: *"Sources conflict on a material field (date,
location, severity, actor)."* This ADR treats only **date and event type** as conflicts.
A grounded **severity** change is read as the disruption developing — a closure that
extends from three days to eight is not two sources disagreeing — and new **locations**
and **actors** as additions rather than contradictions.

That is a deliberate reading, not an oversight, but it is looser than the letter of
trigger 3. The mitigations: a severity change must be quoted, `high`/`severe` always go to
a human anyway, and any change to a human-cleared record re-opens its review. If the
stricter reading is wanted — every severity change goes to review — it is a one-line
change in `_merge_into_existing` and is worth making before real sources are enabled.

---

## Also fixed

`_get_or_create_prompt` still registered **v1** in the `prompts` table after the
prompt-caching change moved extraction to **v2**. Every `extractions` row therefore had a
`prompt_id` naming v1 and a `prompt_version` of "v2" — the attribution mismatch ADR-0001
D6 exists to prevent. It now registers the template extraction actually sends, and a test
asserts every extraction's `prompt_id` and `prompt_version` name the same template.

---

## Verified with real models

A follow-up to a demo port-closure notice through real `bge-small` embeddings and real
Sonnet 5 extraction (about $0.02):

- cosine similarity to the original's cluster was **0.9742**, well above the 0.78
  threshold — a second real data point for ADR-0002 D5;
- it merged as an **update**: event count unchanged, severity moderate → high on a
  verbatim quote about an eight-day closure (which CONSTRAINTS.md §5 defines as `high`),
  a new location added on its own quote, and the record returned to review.

## Consequences

**Positive:** one real-world disruption is one record; second sources corroborate instead
of duplicating; every update is quoted; conflicts reach a human.

**Negative, accepted:** clustering quality now directly decides whether reports merge. A
false match would fold an unrelated notice into an event — mitigated because the
comparison flags a different event type or date as a conflict for review rather than
silently merging. The 0.78 threshold and 14-day window remain provisional until Phase 4
fits them against labelled duplicates.
