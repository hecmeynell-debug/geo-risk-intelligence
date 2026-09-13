"""The dashboard.

The unit tests at the top need no database: they pin :func:`safe_url`, which decides
whether a link from an untrusted source is rendered at all. The integration tests render
real pages over pipeline-created records.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from gri.api.ui import safe_url
from gri.ingestion.normalise import normalise_text
from gri.models import ReviewDecision, Source
from tests import factories
from tests.helpers import seed_event


class TestSafeUrl:
    @pytest.mark.parametrize("url", ["https://example.invalid/a", "http://example.invalid/b?c=1"])
    def test_http_links_are_kept(self, url: str) -> None:
        assert safe_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            " javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "/relative/path",
            "https://",
            "",
            None,
            42,
        ],
    )
    def test_anything_else_is_refused(self, url: object) -> None:
        """A hostile feed must not be able to plant a link that runs code when clicked."""
        assert safe_url(url) is None

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert safe_url("  https://example.invalid/a  ") == "https://example.invalid/a"


integration = pytest.mark.integration


@integration
class TestFeed:
    def test_the_feed_shows_established_records_only(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        shown = seed_event(session, source, severity="moderate", confidence=0.9)
        hidden = seed_event(session, source, severity="severe", confidence=0.95)

        page = client.get("/ui")

        assert page.status_code == 200
        assert f"/ui/events/{shown.event_id}" in page.text
        assert f"/ui/events/{hidden.event_id}" not in page.text
        assert "1 record awaiting human review" in page.text

    def test_the_feed_filters(self, client: TestClient, session: Session, source: Source) -> None:
        seed_event(session, source, severity="moderate")
        assert "Example Port" in client.get("/ui", params={"severity": "moderate"}).text
        assert "No established records match" in client.get("/ui", params={"severity": "low"}).text

    def test_empty_filter_fields_are_ignored(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        """A submitted form sends '' for untouched fields; that must mean 'any'."""
        seed_event(session, source, severity="moderate")
        page = client.get("/ui", params={"event_type": "", "severity": "", "country": ""})
        assert page.status_code == 200
        assert "Example Port" in page.text


@integration
class TestTheEventPage:
    def test_shows_every_quote_with_its_verification(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="moderate")

        page = client.get(f"/ui/events/{event.event_id}").text

        assert "closed to all traffic following a channel obstruction" in page
        assert "verified: exact_normalised_substring" in page
        assert "source document" in page

    def test_a_flagged_record_is_labelled_as_not_established(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)
        page = client.get(f"/ui/events/{event.event_id}").text
        assert "Awaiting human review" in page
        assert "not an established record" in page

    def test_quotes_are_escaped(self, client: TestClient, session: Session, source: Source) -> None:
        """Source text is untrusted. A quote must render as text, never as markup."""
        payload = "<script>alert(1)</script>"
        text = normalise_text(
            f"{payload} The port of Example is closed to all traffic"
            " following a channel obstruction."
        )
        result = factories.make_result(
            quotes=[("summary", f"{payload} The port of Example is closed")]
        )
        event = seed_event(session, source, text=text, result=result)

        page = client.get(f"/ui/events/{event.event_id}").text

        assert payload not in page
        assert "&lt;script&gt;" in page

    def test_a_javascript_source_url_is_not_rendered_as_a_link(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, url="javascript:alert(document.cookie)")
        page = client.get(f"/ui/events/{event.event_id}").text
        assert 'href="javascript:' not in page.lower()
        assert "source link unavailable" in page

    def test_unknown_event_is_404(self, client: TestClient) -> None:
        assert client.get("/ui/events/00000000-0000-0000-0000-000000000000").status_code == 404


@integration
class TestTheBriefingPage:
    def test_renders_records_with_their_quotes(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="moderate")
        page = client.get("/ui/briefing", params={"date": "2026-09-01"}).text
        assert "Daily briefing — 2026-09-01" in page
        assert "closed to all traffic" in page

    def test_says_when_it_is_partial(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="severe", confidence=0.95)
        page = client.get("/ui/briefing", params={"date": "2026-09-01"}).text
        assert "This briefing is partial" in page


@integration
def test_the_locations_page_renders_counts_without_a_map(
    client: TestClient, session: Session, source: Source
) -> None:
    seed_event(session, source)
    page = client.get("/ui/locations").text
    assert "Example Port" in page
    assert "There is no map on purpose" in page


@integration
class TestTheReviewPages:
    def test_the_queue_lists_flagged_records(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)
        page = client.get("/ui/review").text
        assert f"/ui/review/{event.event_id}" in page
        assert "no authentication" in page

    def test_a_blank_reason_is_not_recorded(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)

        response = client.post(
            f"/ui/review/{event.event_id}",
            data={"reviewer": "analyst", "decision": "approve", "reason": "   "},
        )

        assert response.status_code == 200
        assert "Not recorded." in response.text
        assert "reason must not be blank" in response.text
        assert session.scalars(select(ReviewDecision)).all() == []

    def test_an_approval_is_recorded_and_explains_the_kept_flag(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)

        response = client.post(
            f"/ui/review/{event.event_id}",
            data={"reviewer": "analyst", "decision": "approve", "reason": "Checked every quote."},
        )

        assert "Decision recorded" in response.text
        assert "keeps its review flag" in response.text
        assert len(session.scalars(select(ReviewDecision)).all()) == 1

    def test_a_rejected_edit_keeps_what_the_reviewer_typed(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)

        response = client.post(
            f"/ui/review/{event.event_id}",
            data={
                "reviewer": "analyst",
                "decision": "edit",
                "reason": "Typed carefully.",
                "edit_title": "short",
            },
        )

        assert "Not recorded." in response.text
        assert "Typed carefully." in response.text
        assert event.title != "short"
        assert session.scalars(select(ReviewDecision)).all() == []

    def test_an_edit_lowering_severity_clears_the_flag(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)

        response = client.post(
            f"/ui/review/{event.event_id}",
            data={
                "reviewer": "analyst",
                "decision": "edit",
                "reason": "72h is moderate.",
                "edit_severity": "moderate",
            },
        )

        assert "Decision recorded" in response.text
        assert "review flag is cleared" in response.text
        assert event.requires_human_review is False
