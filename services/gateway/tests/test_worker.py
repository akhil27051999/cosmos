"""
Worker tests — the async core's behaviour, exercised without Redis/Postgres.

They call `process_job` directly against the in-memory adapters, which is where
the interesting logic lives: the state machine, the event log, idempotency, and
cancellation.
"""

from __future__ import annotations

import uuid

import pytest

from app.domain.job import JobStatus
from app.worker import ProcessResult, process_job

pytestmark = pytest.mark.asyncio


async def _noop_work(job):
    """Instant, always-succeeds work (real work is simulated in the worker)."""
    return None


async def _event_types(env, job_id):
    return [e.type.value for e in await env.events.list_for_job(job_id)]


async def test_pending_job_runs_to_succeeded(env):
    job, _ = await env.service.submit({"task": "x"}, None, env.tenant_id)

    result = await process_job(job.id, repo=env.repo, events=env.events, work_fn=_noop_work)

    assert result is ProcessResult.SUCCEEDED
    assert (await env.repo.get(job.id)).status == JobStatus.SUCCEEDED
    # full lifecycle recorded in order
    assert await _event_types(env, job.id) == ["submitted", "started", "succeeded"]


async def test_redelivery_is_idempotent(env):
    job, _ = await env.service.submit({"task": "x"}, None, env.tenant_id)

    first = await process_job(job.id, repo=env.repo, events=env.events, work_fn=_noop_work)
    second = await process_job(job.id, repo=env.repo, events=env.events, work_fn=_noop_work)

    assert first is ProcessResult.SUCCEEDED
    assert second is ProcessResult.SKIPPED          # already terminal -> no-op
    # no duplicate started/succeeded events from the second delivery
    assert await _event_types(env, job.id) == ["submitted", "started", "succeeded"]


async def test_cancel_before_pickup_is_skipped(env):
    job, _ = await env.service.submit({"task": "x"}, None, env.tenant_id)
    await env.service.cancel(job.id)                 # cancelled while still PENDING

    result = await process_job(job.id, repo=env.repo, events=env.events, work_fn=_noop_work)

    assert result is ProcessResult.SKIPPED
    assert (await env.repo.get(job.id)).status == JobStatus.CANCELLED
    # never started
    assert await _event_types(env, job.id) == ["submitted", "cancelled"]


async def test_work_failure_marks_failed(env):
    job, _ = await env.service.submit({"task": "x"}, None, env.tenant_id)

    async def boom(job):
        raise RuntimeError("provider exploded")

    result = await process_job(job.id, repo=env.repo, events=env.events, work_fn=boom)

    assert result is ProcessResult.FAILED
    assert (await env.repo.get(job.id)).status == JobStatus.FAILED
    log = await env.events.list_for_job(job.id)
    assert [e.type.value for e in log] == ["submitted", "started", "failed"]
    assert log[-1].data["error"] == "provider exploded"


async def test_orphan_job_id_is_noop(env):
    result = await process_job(
        uuid.uuid4(), repo=env.repo, events=env.events, work_fn=_noop_work
    )
    assert result is ProcessResult.ORPHAN
