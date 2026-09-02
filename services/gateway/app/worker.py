"""
The worker — the second process of the platform (run: `python -m app.worker`).

It consumes job ids from the queue and drives each job through its lifecycle:
    PENDING --start--> RUNNING --(work)--> SUCCEEDED | FAILED
and steps aside if the job was CANCELLED.

Two pieces, deliberately separated:
  * `process_job(...)` — the pure decision logic for ONE job. It's idempotent
    and takes its ports as arguments, so tests drive it with the in-memory
    adapters (no Redis, no Postgres). This is where the interesting behaviour is.
  * `run_worker()` — the real I/O loop: wire up Redis + a DB session per
    message, consume, process, and ACK only after a successful commit.

IDEMPOTENCY (why it matters): the queue is at-least-once, so `process_job` may
see the same job twice — after a crash, or a reclaim. It stays correct because
it inspects the job's current state first: a terminal job is skipped, and it
only *starts* a job that is still PENDING.
"""

from __future__ import annotations

import asyncio
from enum import Enum

import uuid

from app.application.job_service import JobService
from app.domain.exceptions import DomainError
from app.domain.job import JobStatus
from app.ports.event_store import EventStore
from app.ports.job_repository import JobRepository


class ProcessResult(str, Enum):
    ORPHAN = "orphan"            # job id not found (enqueued but not persisted)
    SKIPPED = "skipped"          # already terminal (redelivery or cancelled)
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"      # cancelled part-way through; not completed


async def process_job(job_id: uuid.UUID, *, repo: JobRepository, events: EventStore, work_fn) -> ProcessResult:
    """Handle exactly one delivered job id. Safe to call more than once."""
    job = await repo.get(job_id)
    if job is None:
        # Enqueued but not in the DB — see the outbox note in job_service.py.
        return ProcessResult.ORPHAN
    if job.is_terminal:
        # Already succeeded/failed/cancelled. A redelivery, or it was cancelled
        # before we picked it up. Nothing to do — this is the idempotent guard.
        return ProcessResult.SKIPPED

    service = JobService(repo, events)

    # Only PENDING jobs get "started". A RUNNING job here means a redelivery
    # after a crash mid-run, so we skip the (illegal) RUNNING->RUNNING move and
    # just resume the work.
    if job.status == JobStatus.PENDING:
        job = await service.mark_running(job)

    # Do the actual work.
    try:
        await work_fn(job)
    except Exception as exc:  # noqa: BLE001 - any failure marks the job FAILED
        fresh = await repo.get(job.id)
        if fresh and not fresh.is_terminal:
            try:
                await service.mark_failed(fresh, str(exc))
            except DomainError:
                pass  # someone cancelled it in the meantime — leave it terminal
        return ProcessResult.FAILED

    # Work finished. But it may have been cancelled while we ran, so re-read and
    # only complete if it's still ours to complete.
    fresh = await repo.get(job.id)
    if fresh is None or fresh.is_terminal:
        return ProcessResult.ABANDONED
    try:
        await service.mark_succeeded(fresh)
    except DomainError:
        return ProcessResult.ABANDONED
    return ProcessResult.SUCCEEDED


# ---------------------------------------------------------------------------
# The real loop (I/O). Imports Redis lazily so tests importing this module for
# `process_job` don't need the redis package installed.
# ---------------------------------------------------------------------------

def _make_work_fn(repo: JobRepository):
    """A placeholder unit of work with COOPERATIVE cancellation.

    It "works" in a few steps, re-reading the job between steps; if the job has
    been cancelled it returns early. Cooperative cancellation = the worker
    periodically checks whether it's been asked to stop, rather than being
    force-killed mid-write.
    """

    async def work_fn(job) -> None:
        for _ in range(3):
            current = await repo.get(job.id)
            if current is None or current.is_terminal:
                return  # cancelled/gone -> stop; process_job won't complete it
            await asyncio.sleep(0.3)  # pretend to do something useful

    return work_fn


async def run_worker() -> None:
    import os
    import socket

    from redis.asyncio import Redis

    from app.adapters.postgres.event_store import PostgresEventStore
    from app.adapters.postgres.repository import PostgresJobRepository
    from app.adapters.redis.queue import RedisJobQueue
    from app.core.config import get_settings
    from app.core.db import create_all, get_sessionmaker

    settings = get_settings()
    await create_all()  # ensure tables exist if the worker starts before the API

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    consumer_name = f"{socket.gethostname()}-{os.getpid()}"
    queue = RedisJobQueue(redis, consumer_name)
    await queue.ensure_group()

    sessionmaker = get_sessionmaker()
    print(f"[worker] started as '{consumer_name}', waiting for jobs...")

    # On startup, reclaim anything a previous worker left pending (crash recovery).
    backlog = await queue.reclaim_stale(min_idle_ms=30_000)
    messages = backlog
    while True:
        if not messages:
            messages = await queue.consume(count=10, block_ms=2000)
        for msg in messages:
            async with sessionmaker() as session:
                repo = PostgresJobRepository(session)
                events = PostgresEventStore(session)
                try:
                    result = await process_job(
                        msg.job_id, repo=repo, events=events, work_fn=_make_work_fn(repo)
                    )
                    await session.commit()
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    # Do NOT ack -> the message stays pending and gets redelivered.
                    print(f"[worker] error on {msg.job_id}: {exc!r} (will retry)")
                    continue
            # Ack ONLY after a clean commit — that ordering is what makes the
            # whole thing at-least-once rather than at-most-once.
            await queue.ack(msg.msg_id)
            print(f"[worker] job {msg.job_id} -> {result.value}")
        messages = []


def main() -> None:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        print("\n[worker] shutting down")


if __name__ == "__main__":
    main()
