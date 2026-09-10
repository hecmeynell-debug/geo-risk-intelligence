"""Fetching conduct: conditional requests, backoff, and blocks.

Everything here uses ``httpx.MockTransport``. Ingestion tests never touch the network --
partly for speed and determinism, but mainly because no candidate source has had its
terms reviewed, so the test suite has no business fetching from any of them.
"""

from __future__ import annotations

import httpx
import pytest

from gri.ingestion.http import FetchFailed, HttpFetcher, SourceBlocked

URL = "https://notices.example.invalid/feed.xml"


class RecordingSleep:
    """Captures backoff delays instead of actually waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_fetcher(
    handler: object, sleep: RecordingSleep | None = None, enforce_robots: bool = False
) -> HttpFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return HttpFetcher(
        client=client,
        sleep=sleep or RecordingSleep(),
        enforce_robots=enforce_robots,
        backoff_base_seconds=2.0,
    )


class TestSuccessfulFetch:
    def test_returns_body_and_provenance(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"<rss/>",
                headers={
                    "content-type": "application/rss+xml",
                    "etag": '"abc123"',
                    "last-modified": "Tue, 01 Sep 2026 08:30:00 GMT",
                },
            )

        result = make_fetcher(handler).fetch(URL)

        assert result.status_code == 200
        assert result.body == b"<rss/>"
        assert result.content_type == "application/rss+xml"
        assert result.etag == '"abc123"'
        assert result.last_modified == "Tue, 01 Sep 2026 08:30:00 GMT"
        assert result.retrieved_at.tzinfo is not None
        assert result.not_modified is False

    def test_sends_identifying_user_agent(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, content=b"ok")

        make_fetcher(handler).fetch(URL)
        assert "geo-risk-intelligence" in seen["user-agent"]


class TestConditionalRequests:
    def test_sends_validators_when_we_have_them(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(304)

        make_fetcher(handler).fetch(
            URL, etag='"abc123"', last_modified="Tue, 01 Sep 2026 08:30:00 GMT"
        )

        assert seen["if-none-match"] == '"abc123"'
        assert seen["if-modified-since"] == "Tue, 01 Sep 2026 08:30:00 GMT"

    def test_304_is_reported_as_not_modified(self) -> None:
        result = make_fetcher(lambda request: httpx.Response(304)).fetch(URL, etag='"abc"')

        assert result.not_modified is True
        assert result.status_code == 304
        assert result.body == b""
        # Validators survive a 304 so the next poll stays conditional.
        assert result.etag == '"abc"'

    def test_no_conditional_headers_on_a_first_fetch(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, content=b"ok")

        make_fetcher(handler).fetch(URL)
        assert "if-none-match" not in seen
        assert "if-modified-since" not in seen


class TestBackoff:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_retries_transient_statuses_then_succeeds(self, status: int) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(status)
            return httpx.Response(200, content=b"ok")

        sleep = RecordingSleep()
        result = make_fetcher(handler, sleep).fetch(URL)

        assert result.status_code == 200
        assert result.attempts == 2
        assert len(sleep.delays) == 1

    def test_backoff_grows_exponentially(self) -> None:
        sleep = RecordingSleep()
        with pytest.raises(FetchFailed):
            make_fetcher(lambda request: httpx.Response(503), sleep).fetch(URL)

        assert sleep.delays == [2.0, 4.0, 8.0]

    def test_gives_up_after_max_attempts(self) -> None:
        with pytest.raises(FetchFailed, match="after 4 attempts"):
            make_fetcher(lambda request: httpx.Response(503)).fetch(URL)

    def test_honours_retry_after_seconds(self) -> None:
        sleep = RecordingSleep()
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "17"})
            return httpx.Response(200, content=b"ok")

        make_fetcher(handler, sleep).fetch(URL)
        assert sleep.delays == [17.0]

    def test_retry_after_is_capped(self) -> None:
        """A publisher asking for an hour must not wedge the worker for an hour."""
        sleep = RecordingSleep()
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "3600"})
            return httpx.Response(200, content=b"ok")

        make_fetcher(handler, sleep).fetch(URL)
        assert sleep.delays == [60.0]

    def test_transport_errors_are_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, content=b"ok")

        assert make_fetcher(handler).fetch(URL).attempts == 3


class TestBlocksAreAnswers:
    @pytest.mark.parametrize("status", [401, 403, 451])
    def test_refusal_raises_immediately_without_retrying(self, status: int) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(status)

        sleep = RecordingSleep()
        with pytest.raises(SourceBlocked):
            make_fetcher(handler, sleep).fetch(URL)

        # The point of the policy: one request, no retries, no varying the approach.
        assert calls["n"] == 1
        assert sleep.delays == []

    def test_404_is_a_failure_not_a_block(self) -> None:
        with pytest.raises(FetchFailed):
            make_fetcher(lambda request: httpx.Response(404)).fetch(URL)


class TestRobots:
    def _handler_with_robots(self, robots_body: str):  # type: ignore[no-untyped-def]
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=robots_body)
            return httpx.Response(200, content=b"payload")

        return handler

    def test_disallowed_path_is_refused(self) -> None:
        handler = self._handler_with_robots("User-agent: *\nDisallow: /feed.xml\n")
        with pytest.raises(SourceBlocked, match="robots.txt"):
            make_fetcher(handler, enforce_robots=True).fetch(URL)

    def test_allowed_path_is_fetched(self) -> None:
        handler = self._handler_with_robots("User-agent: *\nDisallow: /private/\n")
        assert make_fetcher(handler, enforce_robots=True).fetch(URL).body == b"payload"

    def test_absent_robots_is_treated_as_permissive(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, content=b"payload")

        assert make_fetcher(handler, enforce_robots=True).fetch(URL).body == b"payload"

    def test_robots_is_fetched_once_per_origin(self) -> None:
        calls = {"robots": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                calls["robots"] += 1
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            return httpx.Response(200, content=b"payload")

        fetcher = make_fetcher(handler, enforce_robots=True)
        fetcher.fetch(URL)
        fetcher.fetch(URL)
        assert calls["robots"] == 1
