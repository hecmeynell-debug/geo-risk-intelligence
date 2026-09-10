"""Source adapters and the raw document store.

Layering, deliberately kept flat:

``http``       fetching, conditional requests, backoff, robots
``base``       the adapter contract and shared parsing helpers
``adapters``   the three engines: RSS/Atom, JSON API, HTML notices
``registry``   per-source configuration -- all candidates, all disabled
``normalise``  text canonicalisation and the hashes dedup depends on
``store``      writing documents with provenance and deduplicating them
``runner``     one source end to end, plus due-source selection
"""

from __future__ import annotations

from gri.ingestion.adapters import HtmlNoticeAdapter, JsonApiAdapter, RssAdapter
from gri.ingestion.base import ParsedItem, SourceAdapter
from gri.ingestion.http import FetchFailed, FetchResult, HttpFetcher, SourceBlocked
from gri.ingestion.normalise import content_hash, metadata_hash, normalise_text
from gri.ingestion.registry import CANDIDATE_SOURCES, SourceConfig, source_by_slug
from gri.ingestion.runner import due_sources, ingest_due_sources, ingest_source
from gri.ingestion.store import StoreOutcome, store_item, store_items

__all__ = [
    "CANDIDATE_SOURCES",
    "FetchFailed",
    "FetchResult",
    "HtmlNoticeAdapter",
    "HttpFetcher",
    "JsonApiAdapter",
    "ParsedItem",
    "RssAdapter",
    "SourceAdapter",
    "SourceBlocked",
    "SourceConfig",
    "StoreOutcome",
    "content_hash",
    "due_sources",
    "ingest_due_sources",
    "ingest_source",
    "metadata_hash",
    "normalise_text",
    "source_by_slug",
    "store_item",
    "store_items",
]
