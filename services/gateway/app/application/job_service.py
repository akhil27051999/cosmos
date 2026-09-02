"""
JobService — the application layer (use cases).

This sits between the outside world (HTTP routes, the worker loop) and the
domain/ports. Its methods are the platform's VERBS: submit, start, succeed,
fail, cancel. Each one coordinates several ports in a single unit of work:

    change the job's state (domain rule)  +  append an event (event log)
    [+ enqueue, for submit]

Keeping this in one class means the API and the worker share identical
lifecycle logic — the rules can't drift between "how the API cancels a job" and
"how the worker completes one".

Note on atomicity: the repository and event store share one DB session, so the
state change and its event commit together (all-or-nothing). The `enqueue`,
however, talks to Redis, which is NOT in that transaction — so a job can, in a
rare crash window, be committed but not enqueued (or vice-versa). The clean fix
is the transactional OUTBOX pattern in Stage 5; for now the worker tolerates the
gap (it treats an unknown job id as a no-op). This is called out on purpose.
"""

from __future__ import annotations

import uuid

from app.domain.events import JobEvent, JobEventType
from app.domain.exceptions import JobNotFound
from app.domain.job import Job, JobStatus
from app.ports.event_store import EventStore
from app.ports.job_queue import JobQueue
from app.ports.job_repository import JobRepository


class JobService:
    def __init__(
        self,
        repo: JobRepository,
        events: EventStore,
        queue: JobQueue | None = None,  # only needed for submit
    ) -> None:
        self._repo = repo
        self._events = events
        self._queue = queue

    # ---- submit (used by the API) ---------------------------------------

    async def submit(self, payload: dict, idempotency_key: str | None) -> tuple[Job, bool]:
        """Create + enqueue a job. Returns (job, created) where `created` is
        False when we replayed an existing job for the same idempotency key.
        """
        if idempotency_key:
            existing = await self._repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing, False  # idempotent replay — do NOT re-enqueue

        job = Job(payload=payload, idempotency_key=idempotency_key)
        try:
            await self._repo.add(job)
        except Exception:
            # Lost a race on the unique idempotency key: fetch and replay.
            if idempotency_key:
                existing = await self._repo.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return existing, False
            raise

        await self._events.append(_event(job, JobEventType.SUBMITTED))
        assert self._queue is not None, "submit() requires a queue"
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
        # Legal from PENDING or RUNNING; raises InvalidTransition if terminal.
        return await self._transition(job, JobStatus.CANCELLED, JobEventType.CANCELLED)

    # ---- the shared core ------------------------------------------------

    async def _transition(
        self, job: Job, to: JobStatus, event_type: JobEventType, data: dict | None = None
    ) -> Job:
        prev_version = job.version          # what we base the OCC write on
        job.transition_to(to)               # domain rule + version bump (may raise)
        # Persist FIRST so a version conflict aborts before we log an event.
        await self._repo.update(job, expected_version=prev_version)
        await self._events.append(_event(job, event_type, data or {}))
        return job


def _event(job: Job, event_type: JobEventType, data: dict | None = None) -> JobEvent:
    return JobEvent(job_id=job.id, type=event_type, data=data or {})
