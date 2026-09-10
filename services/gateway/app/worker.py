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
import json
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


async def process_job(
    job_id: uuid.UUID, *, repo: JobRepository, events: EventStore, work_fn, cache=None, outbox=None
) -> ProcessResult:
    """Handle exactly one delivered job id. Safe to call more than once."""
    job = await repo.get(job_id)
    if job is None:
        # Enqueued but not in the DB — see the outbox note in job_service.py.
        return ProcessResult.ORPHAN
    if job.is_terminal:
        # Already succeeded/failed/cancelled. A redelivery, or it was cancelled
        # before we picked it up. Nothing to do — this is the idempotent guard.
        return ProcessResult.SKIPPED

    # Pass the cache so the worker's own transitions invalidate it too — else an
    # API read could serve a stale status after the worker advances the job.
    # Pass the outbox so started/succeeded/failed events are staged for the bus
    # in the same transaction as the event append.
    service = JobService(repo, events, cache=cache, outbox=outbox)

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

def _make_agent_work_fn(repo: JobRepository, docs, bus, llm, settings):
    """Stage 3: run the real Claude agent loop as the job's unit of work.

    Reads the task from the job's payload (Mongo), runs plan->tool->observe under
    a token budget, streams each step to the StreamBus (for SSE), and stores the
    final answer + full trajectory back to Mongo. Cancellation is cooperative:
    the loop re-checks the job's status between steps via `should_cancel`.

    On a non-successful stop (budget/max_steps/refusal/cancelled) it raises, so
    `process_job` marks the job FAILED with the reason.
    """
    from app.agent.loop import run_agent
    from app.agent.tools import DEFAULT_TOOLS

    async def work_fn(job) -> None:
        doc = await docs.get(job.id)
        payload = doc.payload if doc else {}
        # accept {"prompt": "..."} or {"task": "..."}; fall back to the raw payload
        task = payload.get("prompt") or payload.get("task") or json.dumps(payload)

        async def on_step(step: dict) -> None:
            await bus.publish(job.id, step)          # live-stream to any SSE client

        async def should_cancel() -> bool:
            current = await repo.get(job.id)
            return current is None or current.is_terminal

        result = await run_agent(
            task,
            llm=llm,
            tools=DEFAULT_TOOLS,
            on_step=on_step,
            should_cancel=should_cancel,
            token_budget=settings.agent_token_budget,
            max_steps=settings.agent_max_steps,
            max_tokens=settings.agent_max_tokens,
        )

        # Persist the trajectory + answer (the record lives in Mongo).
        await docs.set_result(
            job.id,
            {
                "answer": result.answer,
                "trajectory": result.trajectory,
                "usage": result.usage,
                "stopped": result.stopped,
            },
        )
        await bus.publish(job.id, {"type": "end", "stopped": result.stopped})

        if result.stopped != "done":
            raise RuntimeError(f"agent stopped: {result.stopped}")

    return work_fn


async def run_worker() -> None:
    import os
    import socket

    from motor.motor_asyncio import AsyncIOMotorClient
    from redis.asyncio import Redis

    from app.adapters.anthropic.llm import AnthropicLLMClient
    from app.adapters.mongo.document_store import MongoDocumentStore
    from app.adapters.postgres.event_store import PostgresEventStore
    from app.adapters.postgres.repository import PostgresJobRepository
    from app.adapters.redis.cache import RedisJobCache
    from app.adapters.postgres.outbox import PostgresOutbox, PostgresOutboxRelay
    from app.adapters.postgres.read_model import PostgresReadModel
    from app.adapters.redis.queue import RedisJobQueue
    from app.adapters.redis.stream_bus import RedisStreamBus
    from app.adapters.redpanda.message_bus import RedpandaMessageBus
    from app.core.config import get_settings
    from app.core.db import create_all, get_sessionmaker
    from app.projector import run_projector
    from app.relay import run_relay

    settings = get_settings()
    await create_all()  # ensure tables exist if the worker starts before the API

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    consumer_name = f"{socket.gethostname()}-{os.getpid()}"
    queue = RedisJobQueue(redis, consumer_name)
    await queue.ensure_group()

    # Stage 2 stores: Mongo for the job document, Redis for the hot-state cache.
    mongo = AsyncIOMotorClient(settings.mongo_url)
    docs = MongoDocumentStore(mongo, settings.mongo_db)
    cache = RedisJobCache(redis)

    # Stage 3/4: build the model provider(s), then WRAP them with resilience.
    # Same LLMClient port throughout — the agent loop can't tell the difference.
    from app.adapters.resilient.llm import BreakerConfig, ResilientLLMClient
    from app.resilience.retry import RetryPolicy

    def _build_provider(name: str):
        if name == "groq":
            from app.adapters.groq.llm import GroqLLMClient
            return GroqLLMClient(settings.groq_model, settings.groq_api_key or None)
        return AnthropicLLMClient(settings.agent_model, settings.anthropic_api_key or None)

    primary = _build_provider(settings.llm_provider)
    fallback = _build_provider(settings.llm_fallback_provider) if settings.llm_fallback_provider else None
    llm = ResilientLLMClient(
        primary,
        fallback,
        retry=RetryPolicy(
            max_attempts=settings.retry_max_attempts, base_delay=settings.retry_base_delay
        ),
        timeout_s=settings.llm_timeout_s,
        breaker=BreakerConfig(
            failure_threshold=settings.breaker_failure_threshold,
            reset_timeout=settings.breaker_reset_s,
        ),
    )
    bus = RedisStreamBus(redis)

    sessionmaker = get_sessionmaker()

    # Stage 5: the outbox relay drains committed outbox rows to the event
    # backbone (Redpanda) in the background, alongside the job loop. In prod this
    # would be its own deployable; co-locating it here keeps local dev simple.
    event_bus = RedpandaMessageBus(settings.redpanda_brokers)
    outbox_relay = PostgresOutboxRelay(sessionmaker)
    asyncio.create_task(run_relay(outbox_relay, event_bus))

    # Stage 5 part 2: the CQRS projector consumes job-events and builds the
    # denormalized read model. Another background task (its own deployable in prod).
    read_model = PostgresReadModel(sessionmaker)
    asyncio.create_task(run_projector(event_bus, read_model))

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
                outbox = PostgresOutbox(session)   # staged events commit with the tx
                try:
                    result = await process_job(
                        msg.job_id,
                        repo=repo,
                        events=events,
                        work_fn=_make_agent_work_fn(repo, docs, bus, llm, settings),
                        cache=cache,
                        outbox=outbox,
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
