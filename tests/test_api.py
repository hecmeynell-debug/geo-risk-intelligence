from __future__ import annotations

from fastapi.testclient import TestClient

from gri.api.main import create_app


def test_root_states_scope_and_limitations() -> None:
    """NG-1: the service must describe itself honestly at its own front door."""
    with TestClient(create_app()) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.json()

    assert body["scope"] == "maritime, energy, and supply-chain disruption events"
    assert body["limitations"]
    assert "not" in body["what_this_is_not"].lower()
    assert "surveillance" in body["what_this_is_not"].lower()


def test_openapi_description_carries_the_disclaimer() -> None:
    with TestClient(create_app()) as client:
        spec = client.get("/openapi.json").json()

    description = spec["info"]["description"].lower()
    assert "comprehensive" in description
    assert "human review" in description
