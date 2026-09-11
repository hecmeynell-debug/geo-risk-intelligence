"""The Phase 3 JSON endpoints: review queue, decisions, briefing, locations.

Run with:  pytest -m integration
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from gri.models import ReviewDecision, Source
from tests.helpers import seed_event

pytestmark = pytest.mark.integration


class TestReviewQueue:
    def test_lists_pending_records_with_reasons(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        flagged = seed_event(session, source, severity="severe", confidence=0.95)
        seed_event(session, source, severity="low", confidence=0.95)

        body = client.get("/review").json()

        assert body["total"] == 1
        item = body["items"][0]
        assert item["event"]["event_id"] == str(flagged.event_id)
        assert any("trigger 4" in r for r in item["reasons"])

    def test_a_decided_record_leaves_the_queue(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)
        client.post(
            f"/review/{event.event_id}/decisions",
            json={"reviewer": "analyst", "decision": "reject", "reason": "Misread."},
        )
        assert client.get("/review").json()["total"] == 0


class TestDecisions:
    def test_recording_a_decision(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)

        response = client.post(
            f"/review/{event.event_id}/decisions",
            json={
                "reviewer": "analyst",
                "decision": "approve",
                "reason": "Quotes verified by hand.",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["review_status"] == "approved"
        # The approval is recorded, and the response says why it could not clear the flag.
        assert body["requires_human_review"] is True
        assert body["flag_kept_because"]

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_a_blank_reason_is_rejected(
        self, client: TestClient, session: Session, source: Source, reason: str
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)
        response = client.post(
            f"/review/{event.event_id}/decisions",
            json={"reviewer": "analyst", "decision": "approve", "reason": reason},
        )
        assert response.status_code == 422
        assert session.scalars(select(ReviewDecision)).all() == []

    def test_editing_the_summary_is_rejected(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)
        response = client.post(
            f"/review/{event.event_id}/decisions",
            json={
                "reviewer": "a",
                "decision": "edit",
                "reason": "r",
                "edits": {"summary": "Mine."},
            },
        )
        assert response.status_code == 422
        assert "summary is not editable" in response.json()["detail"]

    def test_unknown_event_is_404(self, client: TestClient) -> None:
        response = client.post(
            f"/review/{uuid.uuid4()}/decisions",
            json={"reviewer": "a", "decision": "approve", "reason": "r"},
        )
        assert response.status_code == 404

    def test_history_is_returned_oldest_first(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        event = seed_event(session, source, severity="severe", confidence=0.95)
        for reviewer in ("first", "second"):
            client.post(
                f"/review/{event.event_id}/decisions",
                json={"reviewer": reviewer, "decision": "approve", "reason": "ok"},
            )

        history = client.get(f"/review/{event.event_id}/decisions").json()
        assert [h["reviewer"] for h in history] == ["first", "second"]


class TestBriefingEndpoint:
    def test_returns_items_with_quotes(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="moderate")

        body = client.get("/briefing", params={"date": "2026-09-01"}).json()

        assert body["day"] == "2026-09-01"
        assert len(body["items"]) == 1
        assert body["items"][0]["quotes"]
        assert "no model call" in body["generated_from"]

    def test_reports_what_it_withheld(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="severe", confidence=0.95)

        body = client.get("/briefing", params={"date": "2026-09-01"}).json()

        assert body["items"] == []
        assert body["withheld_pending_review"] == 1
        assert body["is_partial"] is True


class TestLocations:
    def test_counts_established_records_per_location(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="moderate")
        seed_event(session, source, severity="low")

        body = client.get("/locations").json()

        assert body["by_location"] == [
            {"name": "Example Port", "location_type": "port", "country_code": "GB", "events": 2}
        ]
        assert body["by_country"] == [{"country_code": "GB", "events": 2}]

    def test_flagged_records_are_excluded_unless_asked_for(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        seed_event(session, source, severity="severe", confidence=0.95)

        assert client.get("/locations").json()["by_location"] == []
        included = client.get("/locations", params={"include_pending_review": True}).json()
        assert included["by_location"][0]["events"] == 1

    def test_the_response_carries_no_coordinates(
        self, client: TestClient, session: Session, source: Source
    ) -> None:
        """NG-4: the view answers 'where was disruption reported', never 'where is it now'."""
        seed_event(session, source)
        body = client.get("/locations").json()

        serialised = str(body).lower()
        for forbidden in ("latitude", "longitude", "lat", "lon", "position", "track"):
            assert f"'{forbidden}'" not in serialised
        assert "no coordinates" in body["scope_note"].lower()
