"""
Test fixtures.

The key trick: we build the real app, then OVERRIDE the `get_job_repository`
dependency to return the in-memory adapter instead of the Postgres one. Every
route, schema, error handler, and business rule is exercised exactly as in
production — only the storage is swapped. That's the ports-and-adapters payoff:
fast, hermetic tests with zero infrastructure.

(`httpx`'s ASGITransport does not fire lifespan events, so the startup
`create_all()` DB call never runs here — tests never touch a database.)
"""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.adapters.memory.repository import InMemoryJobRepository
from app.api.deps import get_job_repository
from app.main import create_app


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    app = create_app()

    # A single in-memory repo instance for the whole test, so state (created
    # jobs) persists across requests within that test.
    repo = InMemoryJobRepository()
    app.dependency_overrides[get_job_repository] = lambda: repo

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
