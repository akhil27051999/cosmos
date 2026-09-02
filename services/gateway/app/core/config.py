"""
12-factor configuration.

Rule (factor III of the 12-factor app): configuration that varies between
environments lives in the ENVIRONMENT, not in code. This module reads env vars
(optionally from a local .env file) into a typed, validated Settings object, so
the rest of the app imports settings instead of calling os.getenv() everywhere.

`get_settings()` is cached with lru_cache so the file/env is parsed once.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # pydantic-settings maps each field to an env var of the same name
    # (case-insensitive). e.g. APP_ENV, DATABASE_URL, DB_ECHO.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"                 # local | dev | prod
    app_name: str = "cosmos-gateway"

    # asyncpg driver URL. Default points at the docker-compose Postgres.
    database_url: str = "postgresql+asyncpg://cosmos:cosmos@localhost:5432/cosmos"
    db_echo: bool = False                  # set true to see every SQL statement

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the singleton Settings, building it on first call."""
    return Settings()
