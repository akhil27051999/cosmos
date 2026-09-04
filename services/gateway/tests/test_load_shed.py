"""
Load-shedding test — the gateway rejects excess submissions with 429 once the
token bucket is empty (admission control). Uses a tiny, non-refilling bucket.
"""

from __future__ import annotations

import pytest

from app.resilience.rate_limiter import TokenBucket

pytestmark = pytest.mark.asyncio


async def test_submit_sheds_excess_with_429(env):
    # 2 tokens, no refill (frozen clock) -> the 3rd submit is shed.
    env.limiter = TokenBucket(capacity=2, refill_per_sec=0.0, now=lambda: 0.0)

    r1 = await env.client.post("/v1/jobs", json={"payload": {"n": 1}})
    r2 = await env.client.post("/v1/jobs", json={"payload": {"n": 2}})
    r3 = await env.client.post("/v1/jobs", json={"payload": {"n": 3}})

    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r3.status_code == 429                      # shed
    assert r3.json()["error"]["code"] == "rate_limited"
    assert r3.headers.get("Retry-After") == "1"
