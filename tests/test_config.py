from __future__ import annotations

import pytest
from pydantic import ValidationError

from gri.config import Settings


def test_rejects_non_psycopg_driver() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://gri:gri@localhost:5432/gri")


def test_accepts_psycopg_driver() -> None:
    settings = Settings(database_url="postgresql+psycopg://gri:gri@localhost:5432/gri")
    assert settings.database_url.startswith("postgresql+psycopg://")


@pytest.mark.parametrize("seconds", [0, 60, 1799, 3601, 86400])
def test_poll_interval_confined_to_30_to_60_minutes(seconds: int) -> None:
    """CONSTRAINTS.md pins the cadence. Config cannot quietly turn this into a scraper."""
    with pytest.raises(ValidationError):
        Settings(poll_interval_seconds=seconds)


@pytest.mark.parametrize("seconds", [1800, 2400, 3600])
def test_poll_interval_accepts_permitted_range(seconds: int) -> None:
    assert Settings(poll_interval_seconds=seconds).poll_interval_seconds == seconds


def test_user_agent_identifies_the_project() -> None:
    assert "geo-risk-intelligence" in Settings().user_agent
