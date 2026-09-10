"""The candidate source registry.

**Nothing here is approved.** Every entry ships `enabled = false` with no terms review
recorded, and the database refuses to enable a source without one (NG-2). A human runs
`scripts/review_source_terms.py` to record what the terms actually say, and only then can
a source be turned on.

Two honesty flags matter and are not decoration:

* ``endpoint_verified`` -- has a human confirmed this URL is the right endpoint and that
  it still exists? Every entry starts False. The URLs below are the publisher's
  documented or conventional location, written from general knowledge, **not** confirmed
  by fetching them.
* ``format_confirmed`` -- has a human looked at a real response and confirmed the adapter
  configuration matches it? Every entry starts False. The field mappings are a starting
  hypothesis to correct during review, not a tested parse.

An entry may only be enabled once both are True and a terms review is recorded. That is
checked by ``scripts/check_source_terms.py``, so an unverified guess cannot quietly reach
production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gri.ingestion.adapters import HtmlNoticeAdapter, JsonApiAdapter, RssAdapter
from gri.ingestion.base import SourceAdapter

#: 30 minutes. CONSTRAINTS.md permits 30-60; we start at the slower-is-safer end of what
#: is useful and can lengthen per source if a publisher asks.
DEFAULT_POLL_SECONDS = 1800


@dataclass(frozen=True)
class SourceConfig:
    """Declarative definition of one candidate source."""

    slug: str
    name: str
    publisher: str
    feed_url: str
    retrieval_method: str
    #: Constructor kwargs for the adapter engine named by ``retrieval_method``.
    adapter_options: dict[str, Any] = field(default_factory=dict)
    homepage_url: str | None = None
    poll_interval_seconds: int = DEFAULT_POLL_SECONDS
    #: Why this source is in the maritime/energy/supply-chain niche (NG-6).
    niche_rationale: str = ""
    #: What a reviewer must check. Surfaced by the terms-review CLI.
    review_notes: str = ""
    endpoint_verified: bool = False
    format_confirmed: bool = False

    def build_adapter(self) -> SourceAdapter:
        if self.retrieval_method == "rss":
            return RssAdapter(**self.adapter_options)
        if self.retrieval_method == "api":
            return JsonApiAdapter(**self.adapter_options)
        if self.retrieval_method == "notice_html":
            return HtmlNoticeAdapter(**self.adapter_options)
        raise ValueError(f"no adapter engine for retrieval_method {self.retrieval_method!r}")


CANDIDATE_SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig(
        slug="nga-msi-broadcast-warnings",
        name="NGA Maritime Safety Information broadcast warnings",
        publisher="US National Geospatial-Intelligence Agency",
        homepage_url="https://msi.nga.mil/",
        feed_url="https://msi.nga.mil/api/publications/broadcast-warn",
        retrieval_method="api",
        adapter_options={
            "items_path": "broadcast-warn",
            "uid_field": "msgNumber",
            "url_field": "msgLink",
            "title_field": "subject",
            "body_field": "text",
            "published_field": "issueDate",
        },
        niche_rationale="Navigational and maritime security warnings affecting shipping lanes.",
        review_notes=(
            "US Government work, so the rights position is likely permissive -- but"
            " confirm the site's terms and the API's stated usage limits before enabling."
            " Confirm the JSON field names against a real response."
        ),
    ),
    SourceConfig(
        slug="uscg-navcen-lnm",
        name="USCG Navigation Center Local Notices to Mariners",
        publisher="US Coast Guard Navigation Center",
        homepage_url="https://www.navcen.uscg.gov/",
        feed_url="https://www.navcen.uscg.gov/local-notice-to-mariners",
        retrieval_method="notice_html",
        adapter_options={
            "item_selector": "table tr",
            "link_selector": "a",
            "date_selector": "td:nth-child(2)",
        },
        niche_rationale="Port, channel, and waterway restrictions affecting US shipping.",
        review_notes=(
            "HTML notices index. Confirm robots.txt allows this path and that the table"
            " selectors match the live markup -- these are a guess."
        ),
    ),
    SourceConfig(
        slug="noaa-nws-marine-warnings",
        name="NOAA/NWS marine and coastal warnings",
        publisher="US National Oceanic and Atmospheric Administration",
        homepage_url="https://www.weather.gov/",
        feed_url="https://api.weather.gov/alerts/active?region_type=marine",
        retrieval_method="api",
        adapter_options={
            "items_path": "features",
            "uid_field": "id",
            "url_field": "properties.uri",
            "title_field": "properties.headline",
            "body_field": "properties.description",
            "published_field": "properties.sent",
        },
        niche_rationale="Weather-driven disruption to ports and coastal transit.",
        review_notes=(
            "api.weather.gov asks for a descriptive User-Agent with contact details --"
            " confirm ours satisfies it. Check the documented rate limits."
        ),
    ),
    SourceConfig(
        slug="eia-petroleum-supply-disruption",
        name="EIA petroleum and energy supply data",
        publisher="US Energy Information Administration",
        homepage_url="https://www.eia.gov/",
        feed_url="https://api.eia.gov/v2/petroleum/sum/snd/data/",
        retrieval_method="api",
        adapter_options={
            "items_path": "response.data",
            "uid_field": "period",
            "url_field": "url",
            "title_field": "series-description",
            "body_field": "value",
            "published_field": "period",
        },
        niche_rationale="Refinery, pipeline, and petroleum supply disruption indicators.",
        review_notes=(
            "Requires a free API key -- an API key is a registration, not a paywall or an"
            " access-control bypass, so it is compatible with NG-2, but confirm the terms"
            " permit automated retrieval and storage. Key goes in .env, never in code."
        ),
    ),
    SourceConfig(
        slug="doe-oe417-electric-incidents",
        name="DOE OE-417 electric emergency incident reports",
        publisher="US Department of Energy",
        homepage_url="https://www.oe.netl.doe.gov/",
        feed_url="https://www.oe.netl.doe.gov/OE417_annual_summary.aspx",
        retrieval_method="notice_html",
        adapter_options={"item_selector": "table tr", "link_selector": "a"},
        niche_rationale="Grid disruption affecting energy supply and industrial offtake.",
        review_notes=(
            "Published as periodic summary files rather than a live feed; the cadence may"
            " make polling pointless. Consider bulk_file retrieval instead, and confirm"
            " the page structure."
        ),
        poll_interval_seconds=3600,
    ),
    SourceConfig(
        slug="ofac-sdn-changes",
        name="OFAC sanctions list changes",
        publisher="US Department of the Treasury, OFAC",
        homepage_url="https://ofac.treasury.gov/",
        feed_url="https://ofac.treasury.gov/system/files/126/sdn.xml",
        retrieval_method="rss",
        niche_rationale=(
            "Public designations of vessels and energy/shipping entities that explain"
            " observed disruption."
        ),
        review_notes=(
            "NG-4 boundary: ingest ONLY as regulatory notices about vessels and"
            " organisations. Individual designations must not create entity rows -- the"
            " schema forbids a person entity_type, so extraction must drop them."
            " sdn.xml is not an RSS feed; a dedicated parser or bulk_file handling is"
            " probably needed. Do not enable until that is resolved."
        ),
        poll_interval_seconds=3600,
    ),
    SourceConfig(
        slug="eu-consolidated-sanctions",
        name="EU consolidated sanctions list",
        publisher="European Union",
        homepage_url="https://www.sanctionsmap.eu/",
        feed_url="https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content",
        retrieval_method="rss",
        niche_rationale="Public designations affecting shipping and energy trade.",
        review_notes=(
            "Same NG-4 boundary as OFAC. Access may require registration; confirm whether"
            " the public endpoint is genuinely open. XML format is not RSS -- needs its"
            " own parser."
        ),
        poll_interval_seconds=3600,
    ),
    SourceConfig(
        slug="entsog-transparency-interruptions",
        name="ENTSOG transparency platform interruptions",
        publisher="European Network of Transmission System Operators for Gas",
        homepage_url="https://transparency.entsog.eu/",
        feed_url="https://transparency.entsog.eu/api/v1/interruptions.json",
        retrieval_method="api",
        adapter_options={
            "items_path": "interruptions",
            "uid_field": "id",
            "url_field": "url",
            "title_field": "pointLabel",
            "body_field": "interruptionType",
            "published_field": "periodFrom",
        },
        niche_rationale="Gas transmission interruptions at named cross-border points.",
        review_notes="Confirm the API terms and the documented field names.",
    ),
    SourceConfig(
        slug="panama-canal-advisories",
        name="Panama Canal advisories to shipping",
        publisher="Autoridad del Canal de Panama",
        homepage_url="https://pancanal.com/",
        feed_url="https://pancanal.com/en/advisories-to-shipping/",
        retrieval_method="notice_html",
        adapter_options={
            "item_selector": "article, .advisory-item, li",
            "link_selector": "a",
            "title_selector": "h2, h3, a",
        },
        niche_rationale="Chokepoint transit restrictions -- draft, booking slots, closures.",
        review_notes=(
            "High-value source for the niche. Selectors are a guess; confirm against the"
            " live page and check robots.txt."
        ),
    ),
    SourceConfig(
        slug="suez-canal-circulars",
        name="Suez Canal Authority circulars",
        publisher="Suez Canal Authority",
        homepage_url="https://www.suezcanal.gov.eg/",
        feed_url="https://www.suezcanal.gov.eg/English/Navigation/Pages/Circulars.aspx",
        retrieval_method="notice_html",
        adapter_options={"item_selector": "table tr, .circular-item", "link_selector": "a"},
        niche_rationale="Chokepoint transit restrictions and toll/convoy changes.",
        review_notes=(
            "Confirm the terms of use and robots.txt. Circulars are often PDFs, so text"
            " extraction may be needed before this source can yield quotable evidence."
        ),
    ),
)


def source_by_slug(slug: str) -> SourceConfig:
    for config in CANDIDATE_SOURCES:
        if config.slug == slug:
            return config
    raise KeyError(f"unknown source slug: {slug}")
