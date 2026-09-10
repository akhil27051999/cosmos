"""
Test fixtures.

One `env` fixture builds the real app but overrides every port with an in-memory
adapter, and exposes those adapters so tests can drive the API over HTTP and also
inspect the queue, event log, document store, cache, and repository directly.
No Postgres, no Redis, no Mongo — the ports-and-adapters design makes it possible.

Stage 6: the app now AUTHENTICATES every job request. The fixture seeds one
tenant + a real API key in the in-memory identity store, and gives the HTTP
client that key as its default `Authorization` header — so existing tests keep
working unchanged, while the real auth dependency runs on every call. Tests that
want to exercise 401/403/quota override the header (or the limiter) per request.

(`httpx`'s ASGITransport doesn't fire lifespan events, so the startup
`create_all()` DB call never runs here.)
"""

from __future__ import annotations

import os

# Must be set BEFORE anything imports app.core.config (main.py reads settings at
# import time). Enable the admin API and JWT auth in tests with known secrets.
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("JWT_HS256_SECRET", "test-jwt-secret")

from types import SimpleNamespace

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.adapters.memory.cache import InMemoryJobCache
from app.adapters.memory.document_store import InMemoryDocumentStore
from app.adapters.memory.event_store import InMemoryEventStore
from app.adapters.memory.identity_store import InMemoryIdentityStore
from app.adapters.memory.message_bus import InMemoryMessageBus
from app.adapters.memory.outbox import InMemoryOutbox
from app.adapters.memory.queue import InMemoryJobQueue
from app.adapters.memory.read_model import InMemoryReadModel
from app.adapters.memory.repository import InMemoryJobRepository
from app.adapters.memory.stream_bus import InMemoryStreamBus
from app.adapters.memory.usage_ledger import InMemoryUsageLedger
from app.adapters.memory.worker_control import InMemoryWorkerControlClient
from app.api.deps import (
    get_document_store,
    get_event_store,
    get_identity_store,
    get_job_cache,
    get_job_queue,
    get_job_repository,
    get_job_service,
    get_outbox_writer,
    get_read_model_store,
    get_stream_bus,
    get_submit_rate_limiter,
    get_usage_ledger,
    get_worker_control,
)
from app.application.job_service import JobService
from app.auth.keys import generate_api_key
from app.core.config import get_settings
from app.domain.identity import Tenant
from app.main import create_app
from app.resilience.rate_limiter import TokenBucket


@pytest_asyncio.fixture
async def env():
    get_settings.cache_clear()  # pick up ADMIN_TOKEN set above
    app = create_app()

    repo = InMemoryJobRepository()
    events = InMemoryEventStore()
    queue = InMemoryJobQueue()
    docs = InMemoryDocumentStore()
    cache = InMemoryJobCache()
    bus = InMemoryStreamBus()
    outbox = InMemoryOutbox()
    event_bus = InMemoryMessageBus()
    read_model = InMemoryReadModel()
    identity = InMemoryIdentityStore()
    usage = InMemoryUsageLedger()
    worker_control = InMemoryWorkerControlClient()

    # Seed one tenant + a real API key. The plaintext is what the client sends;
    # the store holds only the hash (as in production).
    tenant = Tenant(id="t_test", name="Test Tenant", monthly_quota=10_000)
    await identity.add_tenant(tenant)
    api_key, key_record = generate_api_key(tenant.id)
    await identity.add_api_key(key_record)

    service = JobService(repo, events, queue, docs, cache, outbox, identity, usage)
    # Permissive by default so normal tests aren't shed; a test can reassign
    # `env.limiter` to a tiny bucket to exercise load-shedding.
    limiter = TokenBucket(capacity=1_000_000, refill_per_sec=1_000_000)

    ns = SimpleNamespace(
        client=None, repo=repo, events=events, queue=queue, docs=docs,
        cache=cache, bus=bus, outbox=outbox, event_bus=event_bus,
        read_model=read_model, identity=identity, usage=usage, service=service,
        limiter=limiter, tenant_id=tenant.id, api_key=api_key,
        admin_token=os.environ["ADMIN_TOKEN"],
        jwt_secret=os.environ["JWT_HS256_SECRET"],
        worker_control=worker_control, app=app,
    )

    # Point every provider at the shared in-memory instances.
    app.dependency_overrides[get_job_repository] = lambda: repo
    app.dependency_overrides[get_event_store] = lambda: events
    app.dependency_overrides[get_job_queue] = lambda: queue
    app.dependency_overrides[get_document_store] = lambda: docs
    app.dependency_overrides[get_job_cache] = lambda: cache
    app.dependency_overrides[get_stream_bus] = lambda: bus
    app.dependency_overrides[get_outbox_writer] = lambda: outbox
    app.dependency_overrides[get_read_model_store] = lambda: read_model
    app.dependency_overrides[get_identity_store] = lambda: identity
    app.dependency_overrides[get_usage_ledger] = lambda: usage
    app.dependency_overrides[get_worker_control] = lambda: worker_control
    app.dependency_overrides[get_job_service] = lambda: service
    # Read the limiter off the namespace each call, so a test can swap it.
    app.dependency_overrides[get_submit_rate_limiter] = lambda: ns.limiter

    transport = ASGITransport(app=app)
    # Default the client to the seeded key: existing tests stay auth-unaware.
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {api_key}"},
    ) as client:
        ns.client = client
        yield ns
