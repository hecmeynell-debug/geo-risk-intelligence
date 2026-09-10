"""The three adapter engines, against synthetic fixtures.

No network, no real publisher content. See ``tests/fixtures.py`` for why.
"""

from __future__ import annotations

from datetime import UTC, datetime

from gri.ingestion.adapters import HtmlNoticeAdapter, JsonApiAdapter, RssAdapter
from tests import fixtures

BASE = "https://notices.example.invalid/feed.xml"


class TestRssAdapter:
    def test_parses_every_item(self) -> None:
        items = RssAdapter().parse(fixtures.RSS_FEED, BASE)
        assert len(items) == 2

    def test_extracts_provenance_fields(self) -> None:
        first = RssAdapter().parse(fixtures.RSS_FEED, BASE)[0]
        assert first.source_uid == "notice-1001"
        assert first.url == "https://notices.example.invalid/notice/1001"
        assert first.title == "Example Port closed to all traffic"
        assert first.published_at == datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
        assert "closed to all traffic" in (first.body_text or "")

    def test_published_at_is_timezone_aware(self) -> None:
        for item in RssAdapter().parse(fixtures.RSS_FEED, BASE):
            assert item.published_at is not None
            assert item.published_at.tzinfo is not None

    def test_strips_html_from_descriptions(self) -> None:
        second = RssAdapter().parse(fixtures.RSS_FEED, BASE)[1]
        body = second.body_text or ""
        assert "<b>" not in body and "<p>" not in body
        assert "11.5 m" in body

    def test_parses_atom(self) -> None:
        items = RssAdapter().parse(fixtures.ATOM_FEED, BASE)
        assert len(items) == 1
        assert items[0].source_uid == "urn:example:notice:77"
        assert "unplanned outage" in (items[0].body_text or "")

    def test_malformed_feed_does_not_raise(self) -> None:
        """One broken feed must degrade to zero or partial items, never crash a run."""
        assert isinstance(RssAdapter().parse(fixtures.RSS_FEED_MALFORMED, BASE), list)

    def test_empty_feed_yields_nothing(self) -> None:
        assert RssAdapter().parse(fixtures.RSS_FEED_EMPTY, BASE) == []

    def test_garbage_bytes_yield_nothing(self) -> None:
        assert RssAdapter().parse(b"\x00\x01not xml at all", BASE) == []

    def test_relative_links_resolve_against_base(self) -> None:
        feed = fixtures.RSS_FEED.replace(
            b"<link>https://notices.example.invalid/notice/1001</link>",
            b"<link>/notice/1001</link>",
        )
        assert (
            RssAdapter().parse(feed, BASE)[0].url == "https://notices.example.invalid/notice/1001"
        )


class TestJsonApiAdapter:
    def _adapter(self) -> JsonApiAdapter:
        return JsonApiAdapter(**fixtures.JSON_API_OPTIONS)

    def test_follows_the_configured_items_path(self) -> None:
        # Three raw entries, one of which has none of the mapped fields.
        items = self._adapter().parse(fixtures.JSON_API_PAYLOAD, "https://api.example.invalid/v1")
        assert len(items) == 3

    def test_maps_configured_fields(self) -> None:
        first = self._adapter().parse(fixtures.JSON_API_PAYLOAD, "https://api.example.invalid/v1")[
            0
        ]
        assert first.source_uid == "AP-2026-114"
        assert first.title == "Transit slot reduction at Example Canal"
        assert "32 to 24" in (first.body_text or "")
        assert first.published_at == datetime(2026, 9, 4, 9, 15, tzinfo=UTC)

    def test_item_missing_all_fields_degrades_rather_than_crashing(self) -> None:
        last = self._adapter().parse(fixtures.JSON_API_PAYLOAD, "https://api.example.invalid/v1")[
            -1
        ]
        assert last.source_uid is None
        assert last.title is None

    def test_invalid_json_yields_nothing(self) -> None:
        assert self._adapter().parse(b"{not json", "https://api.example.invalid/v1") == []

    def test_wrong_items_path_yields_nothing(self) -> None:
        adapter = JsonApiAdapter(items_path="nope.missing")
        assert adapter.parse(fixtures.JSON_API_PAYLOAD, "https://api.example.invalid/v1") == []

    def test_top_level_list_payload(self) -> None:
        adapter = JsonApiAdapter(items_path="", uid_field="id", url_field="href")
        payload = b'[{"id": "x1", "href": "/a"}, {"id": "x2", "href": "/b"}]'
        items = adapter.parse(payload, "https://api.example.invalid/v1")
        assert [i.source_uid for i in items] == ["x1", "x2"]


class TestHtmlNoticeAdapter:
    def _adapter(self) -> HtmlNoticeAdapter:
        return HtmlNoticeAdapter(**fixtures.HTML_OPTIONS)

    def test_skips_rows_with_no_link(self) -> None:
        """An entry with no URL cannot be cited back to a source, so it is useless."""
        items = self._adapter().parse(fixtures.HTML_NOTICES, "https://canal.example.invalid/")
        assert len(items) == 2

    def test_resolves_relative_links(self) -> None:
        first = self._adapter().parse(fixtures.HTML_NOTICES, "https://canal.example.invalid/")[0]
        assert first.url == "https://canal.example.invalid/circulars/2026-31"

    def test_extracts_title_date_and_body(self) -> None:
        first = self._adapter().parse(fixtures.HTML_NOTICES, "https://canal.example.invalid/")[0]
        assert first.title == "Convoy schedule amended"
        assert first.published_at == datetime(2026, 9, 6, tzinfo=UTC)
        assert "04:00 local" in (first.body_text or "")

    def test_selector_matching_nothing_yields_nothing(self) -> None:
        adapter = HtmlNoticeAdapter(item_selector="div.does-not-exist")
        assert adapter.parse(fixtures.HTML_NOTICES, "https://canal.example.invalid/") == []

    def test_non_html_payload_yields_nothing(self) -> None:
        assert self._adapter().parse(b"just some text", "https://canal.example.invalid/") == []
