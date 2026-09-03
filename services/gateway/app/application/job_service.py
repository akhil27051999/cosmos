"""
JobService — the application layer (use cases).

This sits between the outside world (HTTP routes, the worker loop) and the
domain/ports. Its methods are the platform's VERBS: submit, start, succeed,
fail, cancel. Each one coordinates several ports in a single unit of work.

Stage 2 additions:
  * submit now writes the big `payload` to the document store (MongoDB) and only
    the small metadata to Postgres — polyglot persistence.
  * every state change invalidates the job's cache entry (invalidate-on-write),
    so hot reads never serve a stale status for long.

Consistency note (interview gold): a submit touches THREE stores — Mongo (doc),
Postgres (job row + event), and Redis (queue). Only the Postgres writes share a
transaction. So there are small crash windows (e.g. Mongo doc written but the
Postgres insert fails -> an orphan doc; or committed but not enqueued). We order
writes to fail safe and the worker tolerates an unknown id; the fully-correct
fix is the transactional OUTBOX + change-data-capture in Stage 5.
"""

from __future__ import annotations

import uuid

from app.domain.events import JobEvent, JobEventType
from app.domain.exceptions import JobNotFound
from app.domain.job import Job, JobStatus
from app.ports.cache import JobCache
from app.ports.document_store import JobDocumentStore
from app.ports.event_store import EventStore
from app.ports.job_queue import JobQueue
from app.ports.job_repository import JobRepository


class JobService:
    def __init__(
        self,
        repo: JobRepository,
        events: EventStore,
        queue: JobQueue | None = None,        # needed for submit
        docs: JobDocumentStore | None = None,  # needed for submit
        cache: JobCache | None = None,         # optional; invalidated on writes
    ) -> None:
        self._repo = repo
        self._events = events
        self._queue = queue
        self._docs = docs
        self._cache = cache

    # ---- submit (used by the API) ---------------------------------------

    async def submit(self, payload: dict, idempotency_key: str | None) -> tuple[Job, bool]:
        """Create + enqueue a job. Returns (job, created); created is False when
        we replayed an existing job for the same idempotency key.
        """
        if idempotency_key:
            existing = await self._repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing, False  # idempotent replay — nothing new written

        job = Job(idempotency_key=idempotency_key)  # metadata only; no payload
        assert self._docs is not None, "submit() requires a document store"
        assert self._queue is not None, "submit() requires a queue"

        # Write the big document FIRST, so the payload exists in Mongo before the
        # job becomes visible/enqueued (the worker reads it there). If the
        # Postgres insert below fails, this doc is a harmless orphan.
        await self._docs.create(job.id, payload)

        try:
            await self._repo.add(job)
        except Exception:
            if idempotency_key:
                existing = await self._repo.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return existing, False
            raise

        await self._events.append(_event(job, JobEventType.SUBMITTED))
        await self._queue.enqueue(job.id)
        return job, True

    # ---- lifecycle transitions (used mostly by the worker) --------------

    async def mark_running(self, job: Job) -> Job:
        return await self._transition(job, JobStatus.RUNNING, JobEventType.STARTED)

    async def mark_succeeded(self, job: Job) -> Job:
        return await self._transition(job, JobStatus.SUCCEEDED, JobEventType.SUCCEEDED)

    async def mark_failed(self, job: Job, error: str) -> Job:
        return await self._transition(
            job, JobStatus.FAILED, JobEventType.FAILED, data={"error": error}
        )

    # ---- cancel (used by the API) ---------------------------------------

    async def cancel(self, job_id: uuid.UUID) -> Job:
        job = await self._repo.get(job_id)
        if job is None:
            raise JobNotFound(str(job_id))
        return await self._transition(job, JobStatus.CANCELLED, JobEventType.CANCELLED)

    # ---- the shared core ------------------------------------------------

    async def _transition(
        self, job: Job, to: JobStatus, event_type: JobEventType, data: dict | None = None
    ) -> Job:
        prev_version = job.version          # what we base the OCC write on
        job.transition_to(to)               # domain rule + version bump (may raise)
        await self._repo.update(job, expected_version=prev_version)
        await self._events.append(_event(job, event_type, data or {}))
        # Invalidate-on-write: drop the cached copy so the next read is fresh.
        if self._cache is not None:
            await self._cache.invalidate(job.id)
        return job


def _event(job: Job, event_type: JobEventType, data: dict | None = None) -> JobEvent:
    return JobEvent(job_id=job.id, type=event_type, data=data or {})
