"""The adapter contract.

An adapter's only job is: bytes in, :class:`ParsedItem` list out. It does no HTTP, no
database work, and no judgement about whether the content is in niche -- that is Phase
2's job. Keeping adapters pure makes them testable against fixtures with no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from dateutil import parser as date_parser

from gri.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ParsedItem:
    """One item extracted from a source payload.

    ``body_text`` is the source's own text as published. It is *not* yet normalised --
    the store does that, so that normalisation happens in exactly one place.
    """

    #: The publisher's own identifier: feed GUID, notice number, filing id.
    source_uid: str | None
    url: str
    title: str | None = None
    body_text: str | None = None
    published_at: datetime | None = None
    canonical_url: str | None = None
    language: str | None = None
    #: Adapter-specific fields worth keeping for audit, stored as JSONB.
    extra: dict[str, Any] = field(default_factory=dict)


class SourceAdapter(Protocol):
    """Parses a fetched payload into items.

    ``retrieval_method`` is declared read-only so that frozen dataclasses satisfy the
    protocol -- adapters are immutable configuration, and a settable attribute here would
    exclude exactly the implementations we want.
    """

    @property
    def retrieval_method(self) -> str:
        """Matches ``sources.retrieval_method``."""
        ...

    def parse(self, payload: bytes, base_url: str) -> list[ParsedItem]:
        """Turn raw response bytes into items. Must not raise on a single bad item."""
        ...


def parse_timestamp(value: object) -> datetime | None:
    """Best-effort timestamp parsing, always returning an aware UTC datetime.

    Publishers are inconsistent about formats and about whether they state a timezone.
    A naive timestamp is assumed to be UTC and recorded as such; that assumption is
    visible here rather than buried, because ``published_at`` feeds reporting-lag
    analysis and a silent local-time reading would skew it.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None

    try:
        parsed = date_parser.parse(value)
    except (ValueError, OverflowError, TypeError):
        log.info("timestamp_unparseable", value=value[:120])
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def decode_payload(payload: bytes, encoding: str | None = None) -> str:
    """Decode bytes to text, tolerating the encoding declarations publishers get wrong."""
    for candidate in (encoding, "utf-8", "utf-8-sig", "cp1252", "latin-1"):
        if not candidate:
            continue
        try:
            return payload.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")
