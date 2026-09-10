"""Application settings, loaded from the environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix="GRI_"
    )

    environment: str = "local"
    debug: bool = False

    database_url: str = "postgresql+psycopg://gri:gri@localhost:5432/gri"

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    #: Sent on every outbound fetch. Identifying honestly, with a contact route, is part
    #: of the source policy -- not decoration.
    user_agent: str = (
        "geo-risk-intelligence/0.1 (+https://github.com/example/geo-risk-intelligence)"
    )

    #: CONSTRAINTS.md pins polling to 30-60 minutes.
    poll_interval_seconds: int = Field(default=1800, ge=1800, le=3600)
    http_timeout_seconds: float = 30.0

    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("database_url must use the psycopg 3 driver: postgresql+psycopg://...")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
