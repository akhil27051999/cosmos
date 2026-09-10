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

    # Redis, used as the durable job queue (Stage 1) and the hot-state cache (Stage 2).
    redis_url: str = "redis://localhost:6379/0"

    # MongoDB, holds the large job documents (payload + result) — Stage 2.
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "cosmos"

    # Redpanda/Kafka event backbone — Stage 5 (the outbox relay publishes here).
    redpanda_brokers: str = "localhost:19092"

    # Stage 3 — the agent layer.
    # Pick the model provider: "anthropic" (default) or "groq" (free tier).
    llm_provider: str = "anthropic"
    # Anthropic:
    anthropic_api_key: str = ""                 # from ANTHROPIC_API_KEY; blank => SDK default
    agent_model: str = "claude-opus-5"          # override with AGENT_MODEL=claude-sonnet-5 to cut cost
    # Groq (free): get a key at console.groq.com, set LLM_PROVIDER=groq.
    groq_api_key: str = ""                       # from GROQ_API_KEY
    groq_model: str = "llama-3.3-70b-versatile"  # a Groq model that supports tool use
    # Shared agent guardrails:
    agent_max_tokens: int = 4096                # per model turn
    agent_max_steps: int = 8                    # loop iterations guardrail
    agent_token_budget: int = 20_000            # total-token guardrail across the loop

    # Stage 4 — resilience around the provider call.
    llm_timeout_s: float = 60.0                 # cap on a single model call
    retry_max_attempts: int = 3                 # total tries per provider
    retry_base_delay: float = 0.5               # backoff base (seconds)
    breaker_failure_threshold: int = 5          # consecutive failures -> OPEN
    breaker_reset_s: float = 30.0               # OPEN cooldown before a probe
    llm_fallback_provider: str = ""             # ""|"anthropic"|"groq" — try this if primary fails

    # Stage 4 — edge load-shedding (admission control on POST /v1/jobs).
    submit_rate_per_sec: float = 20.0           # sustained submissions/sec
    submit_burst: int = 40                      # bucket capacity (short bursts)

    # Stage 6 — multi-tenancy & identity.
    # Shared secret guarding the admin bootstrap endpoints (create tenants/keys).
    # BLANK disables the admin API entirely (a missing secret must not mean "open").
    admin_token: str = ""                       # from ADMIN_TOKEN
    # Default monthly submission quota given to a new tenant when none is set.
    default_monthly_quota: int = 1000

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the singleton Settings, building it on first call."""
    return Settings()
