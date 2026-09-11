# Source terms: research notes for the human review

> **This is not a terms review.** It is preparation for one. No source has been enabled,
> no review has been recorded in the `sources` table, and nothing here should be read as
> a conclusion that any publisher permits our use. Under NG-2 that judgement belongs to a
> human, recorded with `scripts/review_source_terms.py`. See
> [source-provenance-policy.md](source-provenance-policy.md).

- **Researched:** 2026-09-11
- **Method:** each candidate's published terms or legal page, and the `robots.txt` of the
  host that would be fetched. **No feed or API content was fetched** — that waits for a
  human review. `robots.txt` rules were evaluated against the exact feed path with Python's
  `urllib.robotparser`, the same parser the fetcher uses.
- **Reliability of quotes:** ENTSOG quotes come from direct text extraction of its terms
  PDF and are exact. `robots.txt` results are exact. Quotes marked *(via fetch tool)* were
  obtained through a summarising web-fetch tool and should be checked against the page
  before being relied on.

---

## Summary

| Source | Feed path vs robots.txt | Terms found? | Preliminary read | Blocker |
|---|---|---|---|---|
| NGA MSI broadcast warnings | robots.txt absent (404) — permissive by convention | Partly: a "Commercial Use Warning" page exists but is a JavaScript app and could not be read | Likely usable; US Government work | **Read `msi.nga.mil/commercial-use` in a browser** |
| USCG NavCen Local Notices | **Allowed** | Policy page returns "Access Denied" to non-browser clients | Likely usable; US Government work | **Read `uscg.mil/disclaim` in a browser**; selectors unverified |
| NOAA/NWS marine alerts | **Disallowed** — `User-agent: *` / `Disallow: /` | Yes — "open data, free to use for any purpose" | Terms permissive; robots.txt forbids | **Policy decision** (see finding 1) |
| EIA petroleum data | robots.txt on the API host returns 403; main site's rules don't cover the API path | Yes — explicitly public domain, attribution requested | Likely usable | Free API key registration |
| DOE OE-417 | Host did not connect (HTTP 000) | Not reached | Endpoint appears dead or moved | **Endpoint unreachable** (see finding 3) |
| OFAC SDN changes | **Allowed** | No reuse terms on the FAQ page; public downloads | Likely usable; US Government work | **Adapter mismatch**: `sdn.xml` is not RSS |
| EU consolidated sanctions | robots.txt absent (404) | Access terms found | **Fails NG-2** | **"Password-protected area"** (see finding 2) |
| ENTSOG interruptions | **Allowed** | Yes — detailed T&C (Rev. 3) | **Most clearly permitted** of the candidates | Citation format must include retrieval date (see finding 5) |
| Panama Canal advisories | **Allowed** (crawl-delay 10s) | Yes | Commercial reproduction prohibited; non-commercial not expressly granted | **Full-text storage question** (see finding 4) |
| Suez Canal circulars | robots.txt absent (404) | None found | Unknown | No terms located; circulars are often PDFs |

**If every open question resolved favourably,** NGA, USCG, EIA, ENTSOG and one of
NOAA/Panama would give the five-source minimum. Without NOAA and Panama, the list is four,
and CONSTRAINTS.md says to ship with fewer and say so rather than lower the bar.

---

## Cross-cutting findings

### 1. NOAA's robots.txt disallows everything, but NWS documents the API for automated use

`https://api.weather.gov/robots.txt` is exactly:

```
User-agent: *
Disallow: /
```

The NWS API documentation *(via fetch tool)* says: "All of the information presented via
the API is intended to be open data, free to use for any purpose", and "A User Agent is
required to identify your application."

These conflict on their face. `robots.txt` is conventionally addressed to crawlers
indexing pages, and this host serves an API NWS explicitly invites programs to call. But
our fetcher treats a `robots.txt` disallow as a block (`SourceBlocked`), so **as the code
stands, enabling NOAA would disable itself on the first fetch.**

This is a policy question, not a code one: *does our robots rule apply to a documented
API whose publisher invites automated use?* Either answer is defensible; it should be
decided once, written into `source-provenance-policy.md`, and — if the answer is "no" —
implemented as an explicit, per-source, reviewed exemption rather than a quiet bypass.

### 2. The EU consolidated sanctions list is behind a login

OpenSanctions' description of the source *(via fetch tool)*: "The data is published in a
password-protected area but the generated download links can be used to programmatically
update the material." Other references say the Financial Sanctions Files platform needs
an "EU Login" account.

The source policy excludes "anything behind a login" categorically. Using a generated
link from a logged-in session would be exactly the kind of credentialed access NG-2
exists to prevent. **Recommendation for the reviewer: remove this candidate from the
registry** unless a genuinely public endpoint is found.

### 3. The DOE OE-417 endpoint does not connect

`https://www.oe.netl.doe.gov/` (and its `robots.txt`) returned no connection (HTTP 000)
from this machine on 2026-09-11. Search results show the page existed, and ORNL's
OpenEnergyHub hosts OE-417 annual summaries. Two cautions: a failed connection from one
machine is not proof the site is gone, and ORNL is a **different publisher** whose terms
would need their own review — it is not a drop-in replacement. The registry already notes
that OE-417 is published as periodic summaries, which suits a bulk-file adapter rather
than 30-minute polling.

### 4. Panama Canal: commercial reproduction prohibited; non-commercial not expressly granted

From `https://pancanal.com/en/terms-of-use/` *(via fetch tool)*: content is "the property
of the Panama Canal Authority" and "their copying, distribution, transmission,
reproduction or publication for commercial or lucrative purposes is prohibited".
Automated access is not addressed; `robots.txt` allows all paths with `Crawl-delay: 10`.

This project is non-commercial, but the terms do not *grant* non-commercial reproduction
either — they only prohibit the commercial kind. Storing full advisory text and quoting it
is reproduction. Two honest options:

- run it with `store_full_text = false` (headline, link, date only) — but then it can
  never supply a quote, so it could never produce an event on its own; or
- ask the ACP for permission, which is the cleanest route for the highest-value source in
  the niche.

### 5. ENTSOG permits storage and API automation, with a required citation format

From the ENTSOG Transparency Platform Privacy & T&C of Use (TRA0394-16, Rev. 3,
24 September 2018), exact text:

> **5.1** Subject to the following provisions, you may download, store and use the
> contents of the ENTSOG TP in good faith and always complying with good business
> practices regarding the re-use of publicly available data, and provided you keep intact
> all trademark, copyright and other proprietary notices indicated.

> **5.2** When quoting information or data from this ENTSOG TP, you shall at least
> indicate the source, and the date of data download/extraction, following this outline
> "ENTSOG TP [DD-MM-YYYY] https://transparency.entsog.eu/".

> **5.6** ENTSOG does not permit automatic extraction of data or other usage that reduces
> the performance of the ENTSOG TP. [...] ENTSOG reserves the right to block [...] a User
> executing unproportioned downloads (7.5 times more than the average User of the same
> category)

> **5.7** The automate download of data is possible via the API tool of the ENTSOG TP, on
> condition that the current Terms and Conditions of Use are fully respected.

Two implementation consequences, if ENTSOG is approved:

- **Clause 5.2 requires the retrieval date in citations.** The evidence panel currently
  shows the source name and *publication* date. For ENTSOG it must also show the date of
  download — which we already store as `raw_documents.retrieved_at`.
- The document is dated 2018. Check it is still the current revision before relying on
  it.

The API user manual reportedly limits each query to 60 seconds and asks for filters on
date range, points and operators. A 30-minute poll of the interruptions endpoint is light
by any measure.

### 6. US Government sources: public domain in general, site terms still to be read

NGA, USCG, NOAA, EIA, OFAC and DOE are US federal agencies. Works of the US Government
are generally not subject to copyright (17 U.S.C. § 105) — general background, not legal
advice, and not a substitute for reading each site's own terms.

- **EIA** says so explicitly *(via fetch tool)*: "U.S. government publications are in the
  public domain and are not subject to copyright protection. You may use and/or
  distribute any of our data [...]". It asks for an acknowledgment including the
  publication date, e.g. "Source: U.S. Energy Information Administration (Oct 2008)."
- **NOAA/NWS** says the API data is "open data, free to use for any purpose" — see
  finding 1 for the robots problem.
- **OFAC**'s FAQ *(via fetch tool)* says "OFAC cannot give specific advice on how to
  design an automated system for downloading its sanctions list data" and gives no reuse
  terms; the files are publicly downloadable.
- **NGA**'s `msi.nga.mil/commercial-use` "Commercial Use Warning" page is a JavaScript
  app and returned no readable content to a non-browser client. **Its title suggests it
  may restrict something; it must be read before NGA is enabled.**
- **USCG**'s site policy (`https://www.uscg.mil/disclaim/`) returned "Access Denied" to a
  non-browser client. Must be read in a browser.

### 7. Two adapter configurations will not work as written

These are registry problems, independent of the terms:

- **OFAC `sdn.xml`** and the **EU list** are configured with the RSS adapter, but they are
  XML schemas, not RSS. The registry's own review notes already flag this. OFAC would need
  an XML or bulk-file adapter.
- **Suez Canal** circulars are reportedly often PDFs. The HTML notice adapter would find
  the links but no quotable text — a PDF text-extraction step would be needed before this
  source could yield evidence.

### 8. The sanctions boundary, restated

Even where a sanctions source passes review, CONSTRAINTS.md NG-4 governs how it is used:
designations of **individuals** must never create entity rows (the schema forbids a
`person` type, and extraction must drop them). Only vessel and organisation designations
that explain shipping or energy disruption are in scope.

---

## Suggested order for the human review

1. **ENTSOG** — clearest terms; confirm Rev. 3 is current; approve; then add the
   retrieval date to citations (finding 5).
2. **EIA** — explicit public-domain statement; register for the free API key.
3. **NGA MSI** — read the Commercial Use Warning page first.
4. **USCG NavCen** — read the site policy; then confirm the HTML selectors against the
   live page (`format_confirmed`).
5. **NOAA** — needs the robots-for-APIs policy decision (finding 1) before anything else.
6. **Panama Canal** — decide metadata-only versus asking the ACP (finding 4).
7. **OFAC** — only worth it once an XML adapter exists (finding 7).
8. **Remove** the EU list (finding 2); **park** DOE OE-417 (finding 3) and Suez (no terms,
   PDFs) unless they can be resolved.

Every step above ends with `scripts/review_source_terms.py`, which records who reviewed
what and refuses to enable a source until the terms review, the endpoint check, and the
format check are all recorded.

## Sources consulted

- NWS API documentation: https://www.weather.gov/documentation/services-web-api
- EIA copyright and reuse: https://www.eia.gov/about/copyrights_reuse.php
- ENTSOG TP Privacy & T&C of Use (Rev. 3): https://transparency.entsog.eu/pdf/TRA0394_20161115_ENTSOG_TP_Privacy_TC_of_Use_Rev_3.pdf
- ENTSOG privacy policy and terms: https://www.entsog.eu/privacy-policy-and-terms-use
- Panama Canal terms of use: https://pancanal.com/en/terms-of-use/
- OFAC list file formats and downloads FAQ: https://ofac.treasury.gov/faqs/topic/1641
- OFAC Sanctions List Service: https://ofac.treasury.gov/sanctions-list-service
- NGA MSI commercial use warning (unreadable without a browser): https://msi.nga.mil/commercial-use
- USCG site policy (access denied without a browser): https://www.uscg.mil/disclaim/
- OpenSanctions on the EU FSF source: https://www.opensanctions.org/datasets/eu_fsf/
- OE-417 annual summaries (ORNL): https://openenergyhub.ornl.gov/explore/dataset/oe-417-annual-summaries/
- `robots.txt` for each host, fetched directly on 2026-09-11
