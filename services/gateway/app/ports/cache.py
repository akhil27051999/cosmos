"""
The JobCache port — a fast, expendable copy of a job for hot reads.

Clients poll `GET /v1/jobs/{id}` a lot while waiting for a job. Hitting Postgres
on every poll is wasteful, so we keep a short-lived copy in Redis and read that
first (the CACHE-ASIDE pattern). The cache is never the source of truth — it can
be wiped at any time and the system still works by falling back to Postgres.

Correctness comes from INVALIDATE-ON-WRITE: whenever a job's state changes, the
writer deletes the cached copy, so the next read repopulates from Postgres.

Adapters: Redis (real) and in-memory (tests). The port deals in the domain
`Job` so callers never see serialization details.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from app.domain.job import Job


class JobCache(Protocol):
    async def get(self, job_id: uuid.UUID) -> Job | None:
        """Return the cached job, or None on a miss."""
        ...

    async def set(self, job: Job) -> None:
        """Cache a job (typically with a short TTL)."""
        ...

    async def invalidate(self, job_id: uuid.UUID) -> None:
        """Drop the cached copy — called on every write to that job."""
        ...
