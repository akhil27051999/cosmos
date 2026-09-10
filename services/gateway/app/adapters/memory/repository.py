"""
In-memory adapter for JobRepository.

This is a full, working implementation of the port that stores jobs in a plain
dict. It exists for two reasons:

  1. It proves the value of the port: the API and tests talk to `JobRepository`
     and neither knows nor cares that this one keeps data in RAM.
  2. Tests use it so they run in milliseconds with no Postgres, no Docker, no
     network. (See tests/.)

Because it implements the same interface as the Postgres adapter, you can read
this first to understand *what* the repository must do, then read the Postgres
one to see *how* it does the same thing against a real database.
"""

from __future__ import annotations

import copy
import uuid

from app.domain.exceptions import JobNotFound, VersionConflict
from app.domain.job import Job


class InMemoryJobRepository:
    def __init__(self) -> None:
        # id -> Job. We store *copies* so callers can't mutate our state by
        # holding a reference — the same isolation a real DB gives you for free.
        self._jobs: dict[uuid.UUID, Job] = {}

    async def add(self, job: Job) -> None:
        self._jobs[job.id] = copy.deepcopy(job)

    async def get(self, job_id: uuid.UUID, tenant_id: str | None = None) -> Job | None:
        found = self._jobs.get(job_id)
        if found is None:
            return None
        # Tenant isolation: a row belonging to another tenant reads as "missing".
        if tenant_id is not None and found.tenant_id != tenant_id:
            return None
        return copy.deepcopy(found)

    async def get_by_idempotency_key(self, key: str, tenant_id: str) -> Job | None:
        for job in self._jobs.values():
            if job.idempotency_key == key and job.tenant_id == tenant_id:
                return copy.deepcopy(job)
        return None

    async def list(
        self, limit: int = 50, offset: int = 0, tenant_id: str | None = None
    ) -> list[Job]:
        values = self._jobs.values()
        if tenant_id is not None:
            values = [j for j in values if j.tenant_id == tenant_id]
        ordered = sorted(values, key=lambda j: j.created_at, reverse=True)
        return [copy.deepcopy(j) for j in ordered[offset : offset + limit]]

    async def update(self, job: Job, expected_version: int) -> Job:
        current = self._jobs.get(job.id)
        if current is None:
            raise JobNotFound(str(job.id))
        # The OCC check: refuse the write if the stored version drifted from the
        # version the caller based their change on.
        if current.version != expected_version:
            raise VersionConflict(
                f"expected version {expected_version}, but stored is {current.version}"
            )
        self._jobs[job.id] = copy.deepcopy(job)
        return copy.deepcopy(job)
