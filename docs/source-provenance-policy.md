# Source and Provenance Policy

Governed by [`CONSTRAINTS.md`](../CONSTRAINTS.md). This document defines which sources
may be ingested, what must be recorded for every document, and the gate an adapter must
pass before it is allowed to run.

---

## 1. Source eligibility

A source is eligible only if **all** of the following are true:

1. **Public.** Reachable without credentials, payment, or membership.
2. **Terms permit the use.** The publisher's terms of use, robots policy, or licence
   allow automated retrieval and the storage of retrieved content for this purpose.
3. **Stable and identifiable.** A durable feed or endpoint with a named publisher.
4. **In niche.** Publishes material bearing on maritime, energy, or supply-chain
   disruption (see `CONSTRAINTS.md` Section 3).
5. **Attributable.** Each item carries, or lets us derive, a canonical URL and a
   publication timestamp.

### Categorically excluded

- Paywalled or subscription content, and anything behind a login.
- Any source whose terms prohibit automated access, or whose `robots.txt` disallows the
  path we would fetch.
- Content obtained by circumventing an access control, rate limit, or bot check.
- Personal social media accounts and person-level data of any kind (NG-4).
- Aggregators that republish other outlets' text without the right to do so.
- Commercial newswires by default. Their terms typically restrict redistribution of
  article text. A newswire is only eligible if a specific licence or an explicitly
  permissive feed policy is recorded, and even then we prefer to store the headline,
  link, and publication time rather than full body text.

---

## 2. The approval gate

**An adapter ships with `sources.enabled = false` and cannot be turned on until a terms
review is recorded.** This is the mechanical enforcement of NG-2.

Enabling a source requires **three** independent things to be true, all enforced by a
database `CHECK` (`ck_sources_enabled_requires_terms_review` and
`ck_sources_enabled_requires_verified_adapter`):

1. **A terms review is recorded** — a human read the terms and wrote down what they say.
2. **`endpoint_verified`** — a human confirmed the feed URL is the right one and exists.
3. **`format_confirmed`** — a human looked at a real response and confirmed the adapter
   configuration actually parses it.

Points 2 and 3 exist because the registry's adapter configuration is a *starting
hypothesis*, written from the publisher's documented shape rather than from an observed
response. Shipping an enabled source on an unconfirmed parse would mean silently storing
whatever the guess happened to extract. See `src/gri/ingestion/registry.py`.

To enable a source, a human must record in the `sources` row:

| Field | Meaning |
|---|---|
| `terms_url` | Link to the terms/licence page that was actually read |
| `terms_reviewed_at` | Timestamp of the review |
| `terms_reviewed_by` | Who read them |
| `terms_note` | What the terms say about automated access, storage, and redistribution |
| `licence` | Licence or rights status (e.g. public domain, CC-BY, "permitted, no redistribution of full text") |
| `robots_checked_at` | When robots.txt was last checked for the fetched path |
| `store_full_text` | Whether we are permitted to store the full body, or link + metadata only |

`scripts/check_source_terms.py` fails CI if any row has `enabled = true` while
`terms_reviewed_at` is null, or while the endpoint or format is unverified. The database
carries `CHECK` constraints to the same effect, so an unreviewed source cannot be enabled
even by direct SQL.

The operator flow is `scripts/review_source_terms.py`:

```bash
python scripts/review_source_terms.py --list          # what is registered, and its state

python scripts/review_source_terms.py --slug <slug> \
    --reviewer "A Human" --terms-url <url> --note "what the terms actually say"

python scripts/review_source_terms.py --slug <slug> \
    --endpoint-verified --format-confirmed --store-full-text --enable
```

The script refuses to enable a source that has not cleared all three gates, and reports
which ones are outstanding rather than emitting a constraint violation.

---

## 3. Candidate sources for Phase 1

**Status: all CANDIDATE. None is approved.** These are listed as the shortlist to
review, not as a set of vetted sources. No human has yet reviewed the terms for any of
them, and no claim is made here that their terms permit our use. Each one must pass
Section 2 individually before its adapter is enabled.

Preparation for that review — each candidate's published terms and `robots.txt`, quoted,
with the open questions called out — is in
[source-terms-research.md](source-terms-research.md). It is research, not a review: it
records what the pages say, not a decision.

The shortlist deliberately favours official and government publishers, because their
rights status is usually clear and often public domain.

| Candidate | Publisher | Type | Why shortlisted | Terms status |
|---|---|---|---|---|
| NGA Maritime Safety Information / NAVAREA warnings | US NGA | Notices | Navigational and maritime security warnings; US Gov work | **Unreviewed** |
| USCG Navigation Center - Local Notices to Mariners | US Coast Guard | Notices | Port and waterway restrictions | **Unreviewed** |
| NOAA / NWS marine and port weather warnings | NOAA | Feed | Weather-driven port and transit disruption | **Unreviewed** |
| EIA energy disruption and outage reporting | US EIA | API | Refinery, pipeline, and supply disruption data | **Unreviewed** |
| DOE OE-417 electric emergency incident reports | US DOE | Filings | Grid disruption affecting energy supply | **Unreviewed** |
| OFAC sanctions list changes (shipping/energy scope) | US Treasury | List | Public designations affecting shipping and energy | **Unreviewed** |
| EU consolidated sanctions list | EU | List | Public designations affecting shipping and energy | **Unreviewed** |
| ENTSO-G / ENTSO-E transparency platform | ENTSO | API | Gas and electricity flow interruptions | **Unreviewed** |
| Panama Canal Authority advisories to shipping | ACP | Notices | Chokepoint transit restrictions | **Unreviewed** |
| Suez Canal Authority circulars | SCA | Notices | Chokepoint transit restrictions | **Unreviewed** |

Phase 1 selects **5-10** of these, in that order of preference, and only those that pass
the gate. If fewer than five pass, we ship with fewer and say so in the README rather
than lowering the bar.

Note on sanctions and designation lists: these are ingested as **public regulatory
notices about entities and vessels**, to explain shipping and energy disruption. They
are never used to build person-level records (NG-4). The `entities` table cannot store a
`person` row.

---

## 4. Provenance recorded for every document

Recorded on every row in `raw_documents`, without exception:

| Field | Meaning |
|---|---|
| `source_id` | Which approved source it came from |
| `source_uid` | The publisher's own identifier for the item (feed GUID, notice number) |
| `url` / `canonical_url` | Where it came from, and the canonical form |
| `published_at` | Publication time as stated by the source |
| `retrieved_at` | **Ingestion time** - when we actually fetched it |
| `retrieval_method` | `rss`, `api`, `notice_html`, `bulk_file` |
| `http_status`, `content_type`, `etag`, `last_modified` | Fetch metadata for auditability |
| `raw_hash` | SHA-256 of the exact bytes retrieved |
| `content_hash` | SHA-256 of the normalised text, used for deduplication |
| `clean_text` | Normalised text that citation verification runs against |
| `fetch_run_id` | Links back to the `ingestion_runs` row that fetched it |

**`published_at` and `retrieved_at` are always distinct fields.** Conflating them
destroys the ability to reason about reporting lag, and reporting lag is a real signal in
this domain.

### Hashing rules

One HTTP response usually contains **many** items, so hashes are computed per item rather
than per response — a feed-level hash would make every item in a feed indistinguishable.

- `raw_hash` = SHA-256 over the item's text exactly as published, before normalisation.
- `content_hash` = SHA-256 over `clean_text` after normalisation: Unicode NFKC, CRLF to
  LF, invisible/bidirectional control characters removed, runs of whitespace collapsed to
  a single space, leading/trailing whitespace stripped.
- Where `store_full_text = false` there is no body to hash, so **both** hashes fall back
  to the normalised composite of title, URL, and publication time, joined by a separator
  that cannot occur in normalised text. Such a document is still deduplicable but can
  never yield a quotable citation.
- The response itself stays traceable through `fetch_run_id` and the `extra` column,
  which record which `ingestion_runs` row and HTTP response the item came from.

Deduplication is on `(source_id, content_hash)`, enforced by a unique constraint — so
idempotency holds against a concurrent writer, not merely against a tidy sequential run.
The same item republished unchanged is a duplicate; the same item **re-rendered** with
different whitespace or markup is also a duplicate, because normalisation removes that
difference. The same item with genuinely **edited text** is a new document that may
become an *update* to an existing event — that distinction is Phase 2's job, not
ingestion's.

Normalisation is therefore load-bearing twice over: it decides what counts as a
duplicate, and (from Phase 2) it is the text quotes are verified against. Changing
`gri.ingestion.normalise` is a corpus-wide migration, not a tweak.

---

## 5. Retention and citation integrity

`clean_text` is retained because **citation verification requires the exact text the
quote was drawn from**. A citation that cannot be re-verified against stored text is not
a citation.

- Where `store_full_text = false` for a source, we store only headline, link, publication
  time, and metadata - and that source's items **cannot produce quoted evidence**, so
  they can only ever contribute corroborating links, never `event_evidence` quotes.
- Raw bodies are stored for audit. If a publisher requests removal, the row is tombstoned
  (content nulled, provenance and hashes retained) so the audit trail survives without
  retaining the content.

---

## 6. Fetching conduct

- Identify honestly with a descriptive `User-Agent` including a contact URL.
- Respect `robots.txt` for the fetched path; re-check on the cadence in `robots_checked_at`.
- Honour `ETag` / `Last-Modified` and send conditional requests.
- Poll every 30-60 minutes. No tighter interval, no burst fetching, no parallel hammering
  of one host.
- Back off on `429` and `5xx` with exponential backoff and a retry ceiling.
- Never retry around a block. If a source starts refusing us, the adapter is disabled and
  the source is re-reviewed. **A block is an answer, not an obstacle.**
