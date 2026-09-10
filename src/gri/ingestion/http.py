"""HTTP fetching, conditional requests, backoff, and robots.txt enforcement.

Everything in ``docs/source-provenance-policy.md`` Section 6 lives here:

* identify honestly, with a contact route in the User-Agent
* honour ``ETag`` / ``Last-Modified`` and send conditional requests
* back off on 429 and 5xx, honouring ``Retry-After``
* respect robots.txt for the path we fetch
* **never retry around a block** -- a 401/403 raises :class:`SourceBlocked`, which
  disables the adapter rather than trying again from a different angle
"""

from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx

from gri.config import get_settings
from gri.logging import get_logger

log = get_logger(__name__)

#: Status codes worth retrying: transient server trouble and explicit rate limiting.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Statuses that mean "you are not welcome here". These are answers, not obstacles.
BLOCKED_STATUSES = frozenset({401, 403, 451})

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE_SECONDS = 2.0
#: Never sleep longer than this on a single retry, whatever Retry-After claims.
MAX_BACKOFF_SECONDS = 60.0


class IngestionError(Exception):
    """Base class for ingestion failures."""


class SourceBlocked(IngestionError):
    """The publisher refused us: 401/403/451, or robots.txt disallows the path.

    Deliberately distinct from a transient failure. The runner disables the adapter and
    flags the source for re-review rather than retrying.
    """


class FetchFailed(IngestionError):
    """The fetch failed after exhausting retries."""


@dataclass(frozen=True)
class FetchResult:
    """One HTTP response, with the provenance fields the raw store needs."""

    url: str
    status_code: int
    body: bytes
    content_type: str | None
    etag: str | None
    last_modified: str | None
    retrieved_at: datetime
    #: True when the server answered 304: nothing changed since our last fetch.
    not_modified: bool = False
    attempts: int = 1
    headers: dict[str, str] = field(default_factory=dict)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse ``Retry-After``, which may be seconds or an HTTP date."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(0.0, (target - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


class RobotsPolicy:
    """robots.txt lookups, cached per origin for the life of the object.

    A robots.txt we cannot fetch is treated as permissive, which matches the convention
    every major crawler follows: an origin with no robots.txt has not disallowed
    anything. An origin that *serves* a robots.txt disallowing us is refused.
    """

    def __init__(self, client: httpx.Client, user_agent: str) -> None:
        self._client = client
        self._user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _parser_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._cache:
            return self._cache[origin]

        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            response = self._client.get(urljoin(origin, "/robots.txt"))
            if response.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
            else:
                log.info("robots_absent", origin=origin, status=response.status_code)
        except httpx.HTTPError as exc:
            log.info("robots_unreachable", origin=origin, error=str(exc))

        self._cache[origin] = parser
        return parser

    def allows(self, url: str) -> bool:
        parser = self._parser_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self._user_agent, url)


class HttpFetcher:
    """Polite HTTP client for source adapters.

    Not a general-purpose client: it exists to make the conduct rules in the source
    policy the default behaviour rather than something each adapter has to remember.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        user_agent: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        sleep: object = time.sleep,
        enforce_robots: bool = True,
    ) -> None:
        settings = get_settings()
        self.user_agent = user_agent or settings.user_agent
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent},
        )
        # Always set, never defaulted: httpx supplies its own "python-httpx/x.y" agent,
        # and identifying honestly is the whole point (source policy Section 6).
        self._client.headers["User-Agent"] = self.user_agent
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self._sleep = sleep
        self._robots = RobotsPolicy(self._client, self.user_agent) if enforce_robots else None

    def __enter__(self) -> HttpFetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        """GET ``url``, conditionally if we have validators from a previous fetch.

        Raises :class:`SourceBlocked` if robots.txt disallows the path or the server
        refuses us, and :class:`FetchFailed` if retries are exhausted.
        """
        if self._robots is not None and not self._robots.allows(url):
            raise SourceBlocked(f"robots.txt disallows {url} for {self.user_agent}")

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error: str | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                log.warning("fetch_transport_error", url=url, attempt=attempt, error=last_error)
                if attempt == self.max_attempts:
                    break
                self._backoff(attempt, None)
                continue

            if response.status_code in BLOCKED_STATUSES:
                # A block is an answer. Do not retry, do not vary the request.
                raise SourceBlocked(f"{url} returned {response.status_code}")

            if response.status_code == 304:
                log.info("fetch_not_modified", url=url)
                return FetchResult(
                    url=str(response.url),
                    status_code=304,
                    body=b"",
                    content_type=response.headers.get("content-type"),
                    etag=response.headers.get("etag") or etag,
                    last_modified=response.headers.get("last-modified") or last_modified,
                    retrieved_at=datetime.now(UTC),
                    not_modified=True,
                    attempts=attempt,
                    headers=dict(response.headers),
                )

            if response.status_code in RETRYABLE_STATUSES:
                last_error = f"HTTP {response.status_code}"
                if attempt == self.max_attempts:
                    break
                self._backoff(attempt, _retry_after_seconds(response))
                continue

            if response.status_code >= 400:
                raise FetchFailed(f"{url} returned {response.status_code}")

            return FetchResult(
                url=str(response.url),
                status_code=response.status_code,
                body=response.content,
                content_type=response.headers.get("content-type"),
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                retrieved_at=datetime.now(UTC),
                attempts=attempt,
                headers=dict(response.headers),
            )

        raise FetchFailed(f"{url} failed after {self.max_attempts} attempts: {last_error}")

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        """Exponential backoff, deferring to Retry-After when the server sent one."""
        delay = retry_after if retry_after is not None else self.backoff_base_seconds**attempt
        delay = min(delay, MAX_BACKOFF_SECONDS)
        log.info("fetch_backoff", attempt=attempt, delay_seconds=delay)
        self._sleep(delay)  # type: ignore[operator]
