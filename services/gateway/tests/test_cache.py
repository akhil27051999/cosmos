"""
Tests for the Redis hot-state cache — run against the in-memory cache, which
counts hits/misses so we can assert the cache-aside + invalidate-on-write flow.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_read_is_cache_aside(env):
    job_id = (await env.client.post("/v1/jobs", json={"payload": {"x": 1}})).json()["id"]

    # First read: cache miss -> load from repo -> populate cache.
    await env.client.get(f"/v1/jobs/{job_id}")
    assert env.cache.misses == 1
    assert env.cache.hits == 0

    # Second read: served from the cache.
    await env.client.get(f"/v1/jobs/{job_id}")
    assert env.cache.hits == 1


async def test_write_invalidates_cache(env):
    job_id = (await env.client.post("/v1/jobs", json={"payload": {"x": 1}})).json()["id"]

    # Populate the cache with status "pending".
    r1 = await env.client.get(f"/v1/jobs/{job_id}")
    assert r1.json()["status"] == "pending"

    # A write (cancel) must invalidate the cache...
    await env.client.post(f"/v1/jobs/{job_id}/cancel")

    # ...so the next read reflects the new status, not the stale cached one.
    r2 = await env.client.get(f"/v1/jobs/{job_id}")
    assert r2.json()["status"] == "cancelled"
