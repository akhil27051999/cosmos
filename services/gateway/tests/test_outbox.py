"""
Transactional outbox + relay tests — run against the in-memory outbox and bus.

They prove: (1) every domain event is STAGED in the outbox as part of the same
operation that writes it, and (2) the relay drains the outbox to the bus exactly
once per row and is safe to run repeatedly.
"""

from __future__ import annotations

import pytest

from app.relay import relay_once

pytestmark = pytest.mark.asyncio


async def test_submit_stages_a_submitted_event(env):
    await env.client.post("/v1/jobs", json={"payload": {"task": "x"}})

    rows = env.outbox.all_rows()
    assert len(rows) == 1
    assert rows[0]["topic"] == "job-events"
    assert rows[0]["value"]["type"] == "submitted"
    assert rows[0]["published"] is False          # staged, not yet on the bus


async def test_lifecycle_transitions_are_staged(env):
    job, _ = await env.service.submit({"task": "x"}, None, env.tenant_id)   # -> submitted
    await env.service.mark_running(job)                       # -> started
    await env.service.mark_succeeded(job)                     # -> succeeded

    types = [r["value"]["type"] for r in env.outbox.all_rows()]
    assert types == ["submitted", "started", "succeeded"]


async def test_relay_publishes_then_marks(env):
    await env.service.submit({"task": "x"}, None, env.tenant_id)
    await env.service.submit({"task": "y"}, None, env.tenant_id)
    assert env.outbox.pending_count() == 2

    moved = await relay_once(env.outbox, env.event_bus)

    assert moved == 2
    published = env.event_bus.published("job-events")
    assert len(published) == 2
    assert env.outbox.pending_count() == 0            # all marked published


async def test_relay_is_idempotent_when_nothing_pending(env):
    await env.service.submit({"task": "x"}, None, env.tenant_id)
    await relay_once(env.outbox, env.event_bus)        # publishes 1

    moved_again = await relay_once(env.outbox, env.event_bus)

    assert moved_again == 0                            # nothing left to publish
    assert len(env.event_bus.published("job-events")) == 1   # not re-published
