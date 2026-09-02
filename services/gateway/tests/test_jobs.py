"""API tests (Stage 1). Each documents a behaviour of the accept-fast flow."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_submit_returns_202_and_enqueues(env):
    r = await env.client.post("/v1/jobs", json={"payload": {"task": "summarize"}})
    assert r.status_code == 202               # accepted for async processing
    job = r.json()
    assert job["status"] == "pending"
    assert job["version"] == 1

    # It was placed on the queue for a worker to pick up...
    assert env.queue.depth() == 1
    # ...and a SUBMITTED event was recorded.
    ev = await env.client.get(f"/v1/jobs/{job['id']}/events")
    assert [e["type"] for e in ev.json()] == ["submitted"]


async def test_unknown_job_is_404_with_envelope(env):
    r = await env.client.get("/v1/jobs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "job_not_found"
    assert "correlation_id" in r.json()["error"]


async def test_validation_error_is_422(env):
    r = await env.client.post("/v1/jobs", json={"payload": {}})   # empty payload
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_idempotency_key_replays_without_re_enqueue(env):
    headers = {"Idempotency-Key": "abc-123"}
    r1 = await env.client.post("/v1/jobs", json={"payload": {"n": 1}}, headers=headers)
    r2 = await env.client.post("/v1/jobs", json={"payload": {"n": 2}}, headers=headers)

    assert r1.status_code == 202              # first: accepted
    assert r2.status_code == 200              # second: replayed, not re-created
    assert r1.json()["id"] == r2.json()["id"]
    assert env.queue.depth() == 1            # only enqueued ONCE


async def test_cancel_pending_job(env):
    job = (await env.client.post("/v1/jobs", json={"payload": {"x": 1}})).json()

    r = await env.client.post(f"/v1/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    ev = await env.client.get(f"/v1/jobs/{job['id']}/events")
    assert [e["type"] for e in ev.json()] == ["submitted", "cancelled"]


async def test_cancel_unknown_job_is_404(env):
    r = await env.client.post("/v1/jobs/00000000-0000-0000-0000-000000000000/cancel")
    assert r.status_code == 404


async def test_events_endpoint_404_for_unknown_job(env):
    r = await env.client.get("/v1/jobs/00000000-0000-0000-0000-000000000000/events")
    assert r.status_code == 404
