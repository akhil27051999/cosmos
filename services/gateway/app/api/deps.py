"""
Dependency injection wiring for the web layer.

FastAPI's `Depends` lets a route declare *what* it needs (a JobRepository)
without knowing *how* it's built. These provider functions do the building:
open a DB session for the request, wrap it in the Postgres adapter, and hand it
over. Because the route depends on the port type, tests can override
`get_job_repository` with the in-memory adapter and nothing else changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.postgres.event_store import PostgresEventStore
from app.adapters.postgres.identity_store import PostgresIdentityStore
from app.adapters.postgres.outbox import PostgresOutbox
from app.adapters.postgres.read_model import PostgresReadModel
from app.adapters.postgres.repository import PostgresJobRepository
from app.adapters.postgres.usage_ledger import PostgresUsageLedger
from app.application.job_service import JobService
from app.auth.keys import parse_api_key, verify_secret
from app.core.db import get_sessionmaker
from app.domain.exceptions import Forbidden, Unauthenticated
from app.domain.identity import Principal
from app.ports.cache import JobCache
from app.ports.document_store import JobDocumentStore
from app.ports.event_store import EventStore
from app.ports.identity_store import IdentityStore
from app.ports.job_queue import JobQueue
from app.ports.job_repository import JobRepository
from app.ports.outbox import OutboxWriter
from app.ports.read_model import ReadModelStore
from app.ports.usage_ledger import UsageLedger
from app.resilience.rate_limiter import RateLimiter
from app.ports.stream_bus import StreamBus


async def get_session() -> AsyncIterator[AsyncSession]:
    """One database session per request.

    The `async with` block guarantees the session is closed afterwards. We
    commit if the handler succeeded and roll back if it raised — the classic
    unit-of-work pattern, so a request either fully applies or not at all.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_job_repository(
    session: AsyncSession = Depends(get_session),
) -> JobRepository:
    """Provide the concrete repository the routes will use.

    Return type is the PORT (`JobRepository`), not the concrete class — the
    routes only ever see the interface.
    """
    return PostgresJobRepository(session)


def get_event_store(session: AsyncSession = Depends(get_session)) -> EventStore:
    # FastAPI caches `get_session` within a request, so the repository and the
    # event store share ONE session — their writes commit together.
    return PostgresEventStore(session)


def get_outbox_writer(session: AsyncSession = Depends(get_session)) -> OutboxWriter:
    # Same shared session again -> the outbox row commits atomically with the
    # job/event write (the transactional outbox pattern).
    return PostgresOutbox(session)


def get_read_model_store() -> ReadModelStore:
    # The CQRS read side: its own store (own sessions), independent of the
    # request's write-side session. Reads (stats/view) come from here.
    return PostgresReadModel(get_sessionmaker())


# ---- Stage 6: identity, usage, and authentication -----------------------

def get_identity_store() -> IdentityStore:
    # Own sessions: auth runs as a dependency before the request's write-side
    # session exists, and admin bootstrap is a self-contained write.
    return PostgresIdentityStore(get_sessionmaker())


def get_usage_ledger() -> UsageLedger:
    return PostgresUsageLedger(get_sessionmaker())


def _extract_key(authorization: str | None, x_api_key: str | None) -> str | None:
    """Pull the raw key from `Authorization: Bearer <k>` or `X-API-Key: <k>`."""
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
    if x_api_key:
        return x_api_key.strip()
    return None


async def get_principal(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    identity: IdentityStore = Depends(get_identity_store),
) -> Principal:
    """AUTHENTICATE the request -> a Principal, or raise (401/403).

    The single choke point every protected route depends on. It verifies the
    presented API key against the stored hash, then confirms the owning tenant is
    active. Routes then scope everything to `principal.tenant_id`.
    """
    raw = _extract_key(authorization, x_api_key)
    if raw is None:
        raise Unauthenticated("missing API key")
    parsed = parse_api_key(raw)
    if parsed is None:
        raise Unauthenticated("malformed API key")
    key_id, secret = parsed

    key = await identity.get_api_key(key_id)
    # Same error whether the key-id is unknown, the key is revoked, or the secret
    # is wrong — never reveal WHICH, so an attacker learns nothing.
    if key is None or not key.is_active or not verify_secret(secret, key):
        raise Unauthenticated("invalid API key")

    tenant = await identity.get_tenant(key.tenant_id)
    if tenant is None or not tenant.is_active:
        raise Forbidden("tenant is not active")
    return Principal(tenant_id=key.tenant_id, key_id=key_id)


async def require_admin(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """Guard the admin bootstrap endpoints with a shared secret from config.

    If `ADMIN_TOKEN` is unset, the admin surface is DISABLED (not open) — a
    missing secret must never mean "no auth required".
    """
    from app.core.config import get_settings

    expected = get_settings().admin_token
    if not expected:
        raise Forbidden("admin API is disabled (no ADMIN_TOKEN configured)")
    if not x_admin_token or x_admin_token != expected:
        raise Unauthenticated("invalid admin token")


@lru_cache
def _get_redis():
    """One Redis client (connection pool) per process, created on first use."""
    from redis.asyncio import Redis  # lazy: keeps redis out of test imports

    from app.core.config import get_settings

    return Redis.from_url(get_settings().redis_url, decode_responses=True)


def get_job_queue() -> JobQueue:
    from app.adapters.redis.queue import RedisJobQueue  # lazy import

    # The API only ever enqueues, so its consumer name is cosmetic.
    return RedisJobQueue(_get_redis(), consumer_name="api")


@lru_cache
def _get_mongo_client():
    """One MongoDB client (connection pool) per process, created on first use."""
    from motor.motor_asyncio import AsyncIOMotorClient  # lazy: keep motor out of test imports

    from app.core.config import get_settings

    return AsyncIOMotorClient(get_settings().mongo_url)


def get_document_store() -> JobDocumentStore:
    from app.adapters.mongo.document_store import MongoDocumentStore  # lazy import

    from app.core.config import get_settings

    return MongoDocumentStore(_get_mongo_client(), get_settings().mongo_db)


def get_job_cache() -> JobCache:
    from app.adapters.redis.cache import RedisJobCache  # lazy import

    return RedisJobCache(_get_redis())


@lru_cache
def _get_submit_limiter():
    """One shared, Redis-backed token bucket for admission control on submit.

    Redis-backed so the limit is enforced across ALL gateway replicas, not
    per-process. Created once per process.
    """
    from app.adapters.redis.rate_limiter import RedisTokenBucket  # lazy import

    from app.core.config import get_settings

    s = get_settings()
    return RedisTokenBucket(
        _get_redis(), "ratelimit:submit", s.submit_burst, s.submit_rate_per_sec
    )


def get_submit_rate_limiter() -> RateLimiter:
    return _get_submit_limiter()


def get_stream_bus() -> StreamBus:
    from app.adapters.redis.stream_bus import RedisStreamBus  # lazy import

    return RedisStreamBus(_get_redis())


def get_job_service(
    repo: JobRepository = Depends(get_job_repository),
    events: EventStore = Depends(get_event_store),
    queue: JobQueue = Depends(get_job_queue),
    docs: JobDocumentStore = Depends(get_document_store),
    cache: JobCache = Depends(get_job_cache),
    outbox: OutboxWriter = Depends(get_outbox_writer),
    identity: IdentityStore = Depends(get_identity_store),
    usage: UsageLedger = Depends(get_usage_ledger),
) -> JobService:
    return JobService(repo, events, queue, docs, cache, outbox, identity, usage)
