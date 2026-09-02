"""
Test fixtures.

One `env` fixture builds the real app but overrides every port with the
in-memory adapter, and exposes those adapters so tests can both drive the API
(over HTTP) and inspect/act on the queue, event log, and repository directly.
No Postgres, no Redis — the ports-and-adapters design makes this possible.

(`httpx`'s ASGITransport doesn't fire lifespan events, so the startup
`create_all()` DB call never runs here.)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.adapters.memory.event_store import InMemoryEventStore
from app.adapters.memory.queue import InMemoryJobQueue
from app.adapters.memory.repository import InMemoryJobRepository
from app.api.deps import (
    get_event_store,
    get_job_queue,
    get_job_repository,
    get_job_service,
)
from app.application.job_service import JobService
from app.main import create_app


@pytest_asyncio.fixture
async def env():
    app = create_app()

    repo = InMemoryJobRepository()
    events = InMemoryEventStore()
    queue = InMemoryJobQueue()
    service = JobService(repo, events, queue)

    # Point every provider at the shared in-memory instances.
    app.dependency_overrides[get_job_repository] = lambda: repo
    app.dependency_overrides[get_event_store] = lambda: events
    app.dependency_overrides[get_job_queue] = lambda: queue
    app.dependency_overrides[get_job_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            client=client, repo=repo, events=events, queue=queue, service=service
        )
