"""The three adapter engines.

Deliberately generic and configuration-driven rather than one bespoke module per source.
Ten hand-written parsers for feed formats nobody has inspected would be ten guesses; a
configured adapter makes the guess explicit, reviewable, and correctable in one line once
a human has actually looked at the feed during terms review.

Every adapter is defensive about individual items: one malformed entry is skipped and
logged, never allowed to fail the whole run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import feedparser
from selectolax.parser import HTMLParser

from gri.ingestion.base import ParsedItem, decode_payload, parse_timestamp
from gri.logging import get_logger

log = get_logger(__name__)


def _first_text(node: Any, selector: str | None) -> str | None:
    if not selector:
        return None
    found = node.css_first(selector)
    if found is None:
        return None
    text = found.text(strip=True)
    return text or None


def _dig(payload: Any, path: str) -> Any:
    """Follow a dotted path through nested dicts and lists.

    ``"data.items"`` walks keys; ``"data.0.title"`` indexes a list. Returns None rather
    than raising, so a source that changes shape degrades to missing fields instead of
    crashing the run.
    """
    current = payload
    for segment in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(segment)
        else:
            return None
    return current


# --------------------------------------------------------------------------------------
# RSS / Atom
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RssAdapter:
    """RSS and Atom, via feedparser.

    feedparser is used rather than a strict XML parse because real authority feeds are
    routinely malformed -- undeclared entities, wrong content types, mixed namespaces --
    and it recovers from all of that. It also disables external entity resolution, so a
    hostile feed cannot turn a fetch into a file read.
    """

    retrieval_method: str = "rss"
    #: Prefer the full ``content`` element over ``summary`` when a feed offers both.
    prefer_content: bool = True

    def parse(self, payload: bytes, base_url: str) -> list[ParsedItem]:
        feed = feedparser.parse(payload)
        if feed.bozo and not feed.entries:
            log.warning("rss_unparseable", base_url=base_url, error=str(feed.get("bozo_exception")))
            return []

        items: list[ParsedItem] = []
        for entry in feed.entries:
            try:
                items.append(self._item(entry, base_url))
            except (AttributeError, TypeError, ValueError) as exc:
                log.warning("rss_entry_skipped", base_url=base_url, error=str(exc))
        return items

    def _item(self, entry: Any, base_url: str) -> ParsedItem:
        link = entry.get("link") or ""
        url = urljoin(base_url, link) if link else base_url

        body = None
        if self.prefer_content:
            contents = entry.get("content") or []
            if contents:
                body = contents[0].get("value")
        if not body:
            body = entry.get("summary") or entry.get("description")

        published = (
            entry.get("published")
            or entry.get("updated")
            or entry.get("created")
            or entry.get("pubDate")
        )

        return ParsedItem(
            source_uid=entry.get("id") or entry.get("guid") or link or None,
            url=url,
            title=entry.get("title"),
            body_text=_strip_html(body) if body else None,
            published_at=parse_timestamp(published),
            canonical_url=urljoin(base_url, link) if link else None,
            language=entry.get("language") or None,
            extra={"tags": [t.get("term") for t in entry.get("tags", []) if t.get("term")]},
        )


def _strip_html(markup: str) -> str:
    """Reduce an HTML fragment to its text. Feeds mix escaped HTML into descriptions."""
    if "<" not in markup:
        return markup
    return HTMLParser(markup).text(separator=" ", strip=True)


# --------------------------------------------------------------------------------------
# JSON APIs
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JsonApiAdapter:
    """A JSON endpoint, mapped by dotted field paths.

    Configuring the paths is the honest option: JSON APIs share no conventions, so the
    mapping has to come from looking at a real response. Until someone has, the paths
    below are the source's documented shape and are marked unverified in the registry.
    """

    #: Dotted path to the list of items. Empty string means the payload is the list.
    items_path: str = ""
    uid_field: str = "id"
    url_field: str = "url"
    title_field: str = "title"
    body_field: str = "description"
    published_field: str = "published"
    retrieval_method: str = "api"

    def parse(self, payload: bytes, base_url: str) -> list[ParsedItem]:
        try:
            document = json.loads(decode_payload(payload))
        except json.JSONDecodeError as exc:
            log.warning("json_unparseable", base_url=base_url, error=str(exc))
            return []

        raw_items = _dig(document, self.items_path) if self.items_path else document
        if not isinstance(raw_items, list):
            log.warning("json_items_not_a_list", base_url=base_url, items_path=self.items_path)
            return []

        items: list[ParsedItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            try:
                items.append(self._item(raw, base_url))
            except (TypeError, ValueError) as exc:
                log.warning("json_item_skipped", base_url=base_url, error=str(exc))
        return items

    def _item(self, raw: dict[str, Any], base_url: str) -> ParsedItem:
        link = _dig(raw, self.url_field)
        url = urljoin(base_url, str(link)) if link else base_url
        uid = _dig(raw, self.uid_field)
        body = _dig(raw, self.body_field)

        return ParsedItem(
            source_uid=str(uid) if uid is not None else None,
            url=url,
            title=_as_text(_dig(raw, self.title_field)),
            body_text=_as_text(body),
            published_at=parse_timestamp(_dig(raw, self.published_field)),
            canonical_url=url,
            extra={"raw_keys": sorted(raw)[:40]},
        )


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------------------
# HTML notice pages
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HtmlNoticeAdapter:
    """A notices index page, mapped by CSS selectors.

    Many maritime and canal authorities publish notices as an HTML list with no feed.
    Parsing that is legitimate where the terms permit it -- this reads the page as
    published, at the polling cadence, and does nothing a browser would not.
    """

    item_selector: str
    link_selector: str | None = None
    title_selector: str | None = None
    body_selector: str | None = None
    date_selector: str | None = None
    retrieval_method: str = "notice_html"

    def parse(self, payload: bytes, base_url: str) -> list[ParsedItem]:
        tree = HTMLParser(decode_payload(payload))
        items: list[ParsedItem] = []

        for node in tree.css(self.item_selector):
            try:
                item = self._item(node, base_url)
            except (AttributeError, TypeError, ValueError) as exc:
                log.warning("html_item_skipped", base_url=base_url, error=str(exc))
                continue
            if item is not None:
                items.append(item)
        return items

    def _item(self, node: Any, base_url: str) -> ParsedItem | None:
        anchor = node.css_first(self.link_selector) if self.link_selector else node.css_first("a")
        href = anchor.attributes.get("href") if anchor is not None else None
        if not href:
            # An entry with no link cannot be cited back to a source, so it is useless.
            return None

        url = urljoin(base_url, href)
        title = _first_text(node, self.title_selector)
        if title is None and anchor is not None:
            title = anchor.text(strip=True) or None

        return ParsedItem(
            source_uid=href,
            url=url,
            title=title,
            body_text=_first_text(node, self.body_selector),
            published_at=parse_timestamp(_first_text(node, self.date_selector)),
            canonical_url=url,
        )
