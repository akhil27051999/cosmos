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
from datetime import datetime, timezone

from app.domain.events import JobEvent, JobEventType
from app.domain.exceptions import Forbidden, JobNotFound, QuotaExceeded
from app.domain.job import Job, JobStatus
from app.ports.cache import JobCache
from app.ports.document_store import JobDocumentStore
from app.ports.event_store import EventStore
from app.ports.identity_store import IdentityStore
from app.ports.job_queue import JobQueue
from app.ports.job_repository import JobRepository
from app.ports.outbox import OutboxWriter
from app.ports.usage_ledger import UsageLedger

EVENTS_TOPIC = "job-events"


def _current_period() -> str:
    """The quota bucket for 'now' — the calendar month as 'YYYY-MM' (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


class JobService:
    def __init__(
        self,
        repo: JobRepository,
        events: EventStore,
        queue: JobQueue | None = None,        # needed for submit
        docs: JobDocumentStore | None = None,  # needed for submit
        cache: JobCache | None = None,         # optional; invalidated on writes
        outbox: OutboxWriter | None = None,    # optional; events staged for the bus
        identity: IdentityStore | None = None,  # Stage 6: read the tenant + quota
        usage: UsageLedger | None = None,       # Stage 6: per-tenant consumption
    ) -> None:
        self._repo = repo
        self._events = events
        self._queue = queue
        self._docs = docs
        self._cache = cache
        self._outbox = outbox
        self._identity = identity
        self._usage = usage

    # ---- submit (used by the API) ---------------------------------------

    async def submit(
        self, payload: dict, idempotency_key: str | None, tenant_id: str
    ) -> tuple[Job, bool]:
        """Create + enqueue a job for `tenant_id`. Returns (job, created);
        created is False when we replayed an existing job for the same
        (tenant, idempotency key).
        """
        if idempotency_key:
            existing = await self._repo.get_by_idempotency_key(idempotency_key, tenant_id)
            if existing is not None:
                return existing, False  # idempotent replay — nothing new written

        # Stage 6 QUOTA: check the tenant is active and under its monthly quota
        # BEFORE doing any work. A replayed idempotent submit (above) skips this —
        # it's not new consumption. The check + record aren't one atomic unit, so
        # under heavy concurrency a tenant could overshoot slightly; that's an
        # accepted trade-off for a coarse monthly quota (the ledger increment
        # itself is atomic, so counts are never lost).
        await self._enforce_quota(tenant_id)

        job = Job(tenant_id=tenant_id, idempotency_key=idempotency_key)  # metadata only
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
                existing = await self._repo.get_by_idempotency_key(idempotency_key, tenant_id)
                if existing is not None:
                    return existing, False
            raise

        await self._events.append(_event(job, JobEventType.SUBMITTED))
        await self._stage_event(job, JobEventType.SUBMITTED)   # outbox, same transaction
        await self._queue.enqueue(job.id)

        # Count this accepted job against the tenant's monthly allowance.
        if self._usage is not None:
            await self._usage.record(tenant_id, _current_period(), 1)
        return job, True

    async def _enforce_quota(self, tenant_id: str) -> None:
        """Refuse the submit if the tenant is suspended or over its quota."""
        if self._identity is None or self._usage is None:
            return  # quota not wired (e.g. the worker path) — nothing to enforce
        tenant = await self._identity.get_tenant(tenant_id)
        if tenant is None or not tenant.is_active:
            # A suspended (or vanished) tenant may not submit new work.
            raise Forbidden(f"tenant '{tenant_id}' is not active")
        used = await self._usage.get_usage(tenant_id, _current_period())
        if used >= tenant.monthly_quota:
            raise QuotaExceeded(
                f"monthly quota of {tenant.monthly_quota} reached for tenant '{tenant_id}'"
            )

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

    async def cancel(self, job_id: uuid.UUID, tenant_id: str | None = None) -> Job:
        # Scoped by tenant when called from the API: another tenant's job is
        # simply "not found" (never revealed, never cancellable).
        job = await self._repo.get(job_id, tenant_id)
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
        await self._stage_event(job, event_type)   # outbox, same transaction as the event
        # Invalidate-on-write: drop the cached copy so the next read is fresh.
        if self._cache is not None:
            await self._cache.invalidate(job.id)
        return job

    async def _stage_event(self, job: Job, event_type: JobEventType) -> None:
        """Stage a domain event in the outbox — committed in the caller's
        transaction alongside the event append, so it can never be lost."""
        if self._outbox is not None:
            await self._outbox.add(
                EVENTS_TOPIC,
                str(job.id),
                {
                    "type": event_type.value,
                    "job_id": str(job.id),
                    "tenant_id": job.tenant_id,  # carry the tenant to the read side
                    "status": job.status.value,
                    "version": job.version,   # LWW key for the CQRS projector
                },
            )


def _event(job: Job, event_type: JobEventType, data: dict | None = None) -> JobEvent:
    return JobEvent(job_id=job.id, type=event_type, data=data or {})
