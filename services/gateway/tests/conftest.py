"""
Test fixtures.

One `env` fixture builds the real app but overrides every port with an in-memory
adapter, and exposes those adapters so tests can drive the API over HTTP and also
inspect the queue, event log, document store, cache, and repository directly.
No Postgres, no Redis, no Mongo — the ports-and-adapters design makes it possible.

(`httpx`'s ASGITransport doesn't fire lifespan events, so the startup
`create_all()` DB call never runs here.)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.adapters.memory.cache import InMemoryJobCache
from app.adapters.memory.document_store import InMemoryDocumentStore
from app.adapters.memory.event_store import InMemoryEventStore
from app.adapters.memory.queue import InMemoryJobQueue
from app.adapters.memory.repository import InMemoryJobRepository
from app.adapters.memory.stream_bus import InMemoryStreamBus
from app.api.deps import (
    get_document_store,
    get_event_store,
    get_job_cache,
    get_job_queue,
    get_job_repository,
    get_job_service,
    get_stream_bus,
    get_submit_rate_limiter,
)
from app.application.job_service import JobService
from app.main import create_app
from app.resilience.rate_limiter import TokenBucket


@pytest_asyncio.fixture
async def env():
    app = create_app()

    repo = InMemoryJobRepository()
    events = InMemoryEventStore()
    queue = InMemoryJobQueue()
    docs = InMemoryDocumentStore()
    cache = InMemoryJobCache()
    bus = InMemoryStreamBus()
    service = JobService(repo, events, queue, docs, cache)
    # Permissive by default so normal tests aren't shed; a test can reassign
    # `env.limiter` to a tiny bucket to exercise load-shedding.
    limiter = TokenBucket(capacity=1_000_000, refill_per_sec=1_000_000)

    ns = SimpleNamespace(
        client=None, repo=repo, events=events, queue=queue, docs=docs,
        cache=cache, bus=bus, service=service, limiter=limiter,
    )

    # Point every provider at the shared in-memory instances.
    app.dependency_overrides[get_job_repository] = lambda: repo
    app.dependency_overrides[get_event_store] = lambda: events
    app.dependency_overrides[get_job_queue] = lambda: queue
    app.dependency_overrides[get_document_store] = lambda: docs
    app.dependency_overrides[get_job_cache] = lambda: cache
    app.dependency_overrides[get_stream_bus] = lambda: bus
    app.dependency_overrides[get_job_service] = lambda: service
    # Read the limiter off the namespace each call, so a test can swap it.
    app.dependency_overrides[get_submit_rate_limiter] = lambda: ns.limiter

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ns.client = client
        yield ns
