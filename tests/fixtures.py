"""Synthetic source payloads for tests.

**Every fixture here is invented.** None is a copy of any real publisher's content, and
none was fetched from a live source. That is deliberate: no candidate source has had its
terms reviewed yet, so committing real payloads to the repository would be exactly the
redistribution NG-2 exists to prevent. Invented fixtures also let us test the ugly cases
on purpose -- malformed XML, missing fields, duplicate GUIDs -- which real samples rarely
contain on demand.

Place names are drawn from `example.invalid` style placeholders rather than real ports,
so nothing here can be mistaken for a real advisory if it leaks into a log.
"""

from __future__ import annotations

import json

RSS_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Maritime Authority Notices</title>
    <link>https://notices.example.invalid/</link>
    <item>
      <title>Example Port closed to all traffic</title>
      <link>https://notices.example.invalid/notice/1001</link>
      <guid>notice-1001</guid>
      <pubDate>Tue, 01 Sep 2026 08:30:00 GMT</pubDate>
      <description>Example Port is closed to all traffic following a channel
      obstruction. Vessels should expect delays of up to 72 hours.</description>
    </item>
    <item>
      <title>Draft restriction at Example Terminal</title>
      <link>https://notices.example.invalid/notice/1002</link>
      <guid>notice-1002</guid>
      <pubDate>Tue, 01 Sep 2026 11:00:00 GMT</pubDate>
      <description>&lt;p&gt;Maximum draft reduced to &lt;b&gt;11.5 m&lt;/b&gt;
      with immediate effect.&lt;/p&gt;</description>
    </item>
  </channel>
</rss>
"""

#: Same two notices, re-serialised with different whitespace and attribute order. A
#: publisher re-rendering its feed must not read as new documents.
RSS_FEED_REFORMATTED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Example Maritime Authority Notices</title><link>https://notices.example.invalid/</link>
<item><guid>notice-1001</guid><title>Example Port closed to all traffic</title>
<link>https://notices.example.invalid/notice/1001</link>
<pubDate>Tue, 01 Sep 2026 08:30:00 GMT</pubDate>
<description>Example   Port is closed to all traffic following a channel obstruction.
Vessels should expect delays of up to 72 hours.</description></item>
<item><guid>notice-1002</guid><title>Draft restriction at Example Terminal</title>
<link>https://notices.example.invalid/notice/1002</link>
<pubDate>Tue, 01 Sep 2026 11:00:00 GMT</pubDate>
<description>&lt;p&gt;Maximum draft reduced to &lt;b&gt;11.5 m&lt;/b&gt; with immediate effect.&lt;/p&gt;</description>
</item></channel></rss>
"""

#: One extra notice appended to the original two -- the realistic "feed moved on" case.
RSS_FEED_WITH_NEW_ITEM = RSS_FEED.replace(
    b"  </channel>",
    b"""    <item>
      <title>Example Port reopened to shallow-draft vessels</title>
      <link>https://notices.example.invalid/notice/1003</link>
      <guid>notice-1003</guid>
      <pubDate>Wed, 02 Sep 2026 06:00:00 GMT</pubDate>
      <description>Example Port has reopened to vessels under 8 m draft.</description>
    </item>
  </channel>""",
)

ATOM_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Energy Regulator Notices</title>
  <entry>
    <title>Unplanned outage at Example Refinery</title>
    <link href="https://energy.example.invalid/notice/77"/>
    <id>urn:example:notice:77</id>
    <updated>2026-09-03T14:00:00Z</updated>
    <content type="text">Example Refinery reports an unplanned outage affecting
    approximately 40 percent of capacity.</content>
  </entry>
</feed>
"""

#: Truncated mid-element. feedparser should recover the entries it can and we should not
#: crash the run.
RSS_FEED_MALFORMED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Broken</title>
<item><title>Half an item</title><link>https://notices.example.invalid/notice/9
"""

RSS_FEED_EMPTY = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Nothing today</title></channel></rss>
"""

JSON_API_PAYLOAD = json.dumps(
    {
        "response": {
            "data": [
                {
                    "noticeId": "AP-2026-114",
                    "link": "/advisories/AP-2026-114",
                    "subject": "Transit slot reduction at Example Canal",
                    "text": "Daily transit slots reduced from 32 to 24 with effect from"
                    " 5 September 2026 owing to low water levels.",
                    "issued": "2026-09-04T09:15:00Z",
                },
                {
                    "noticeId": "AP-2026-115",
                    "link": "/advisories/AP-2026-115",
                    "subject": "Draft limit revised at Example Canal",
                    "text": "Maximum authorised draft revised to 13.4 m.",
                    "issued": "2026-09-05T09:15:00Z",
                },
                # Missing every mapped field: must be skipped, not crash the parse.
                {"unrelated": True},
            ]
        }
    }
).encode()

JSON_API_OPTIONS = {
    "items_path": "response.data",
    "uid_field": "noticeId",
    "url_field": "link",
    "title_field": "subject",
    "body_field": "text",
    "published_field": "issued",
}

HTML_NOTICES = b"""<!doctype html>
<html><body>
  <table class="notices">
    <tr class="notice">
      <td><a href="/circulars/2026-31">Convoy schedule amended</a></td>
      <td class="date">2026-09-06</td>
      <td class="summary">Northbound convoy departure moved to 04:00 local.</td>
    </tr>
    <tr class="notice">
      <td><a href="/circulars/2026-32">Tug escort now mandatory</a></td>
      <td class="date">2026-09-07</td>
      <td class="summary">Tug escort mandatory for vessels over 150,000 DWT.</td>
    </tr>
    <tr class="notice">
      <td>No link here, so this row cannot be cited</td>
      <td class="date">2026-09-08</td>
    </tr>
  </table>
</body></html>
"""

HTML_OPTIONS = {
    "item_selector": "tr.notice",
    "link_selector": "a",
    "date_selector": "td.date",
    "body_selector": "td.summary",
}
