"""
CQRS read-model tests — the projector folds job-events into the read model, and
the read endpoints serve it. End-to-end through the in-memory bus:
    service (stage) -> relay (publish) -> projector (apply) -> read model.
"""

from __future__ import annotations

import pytest

from app.projector import project_once
from app.relay import relay_once

pytestmark = pytest.mark.asyncio


async def _drain(env) -> int:
    """Move staged events onto the bus and project them into the read model."""
    await relay_once(env.outbox, env.event_bus)
    return await project_once(env.event_bus, env.read_model)


async def test_projector_builds_the_read_model(env):
    job, _ = await env.service.submit({"x": 1}, None, env.tenant_id)   # submitted (v1)
    await env.service.mark_running(job)                  # started   (v2)
    await env.service.mark_succeeded(job)                # succeeded (v3)

    applied = await _drain(env)

    assert applied == 3
    entry = await env.read_model.get(str(job.id))
    assert entry.status == "succeeded"
    assert entry.version == 3
    assert entry.event_count == 3


async def test_read_model_apply_is_idempotent_and_lww(env):
    ev = {"type": "submitted", "job_id": "J", "status": "pending", "version": 1}
    assert await env.read_model.apply(ev) is True
    assert await env.read_model.apply(ev) is False        # duplicate -> no-op
    assert (await env.read_model.get("J")).event_count == 1   # not double-counted

    # a NEWER version advances the view...
    assert await env.read_model.apply({"type": "x", "job_id": "J", "status": "failed", "version": 5}) is True
    # ...an OLDER (reordered) event is ignored
    assert await env.read_model.apply({"type": "x", "job_id": "J", "status": "running", "version": 2}) is False
    assert (await env.read_model.get("J")).status == "failed"


async def test_stats_endpoint(env):
    await env.service.submit({"a": 1}, None, env.tenant_id)             # stays pending
    j2, _ = await env.service.submit({"b": 2}, None, env.tenant_id)
    await env.service.mark_running(j2)
    await env.service.mark_succeeded(j2)                 # succeeded
    await _drain(env)

    body = (await env.client.get("/v1/stats")).json()
    assert body.get("pending") == 1
    assert body.get("succeeded") == 1


async def test_view_endpoint_and_404(env):
    job, _ = await env.service.submit({"x": 1}, None, env.tenant_id)
    await _drain(env)

    r = await env.client.get(f"/v1/jobs/{job.id}/view")
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert r.json()["event_count"] == 1

    missing = await env.client.get("/v1/jobs/00000000-0000-0000-0000-000000000000/view")
    assert missing.status_code == 404
