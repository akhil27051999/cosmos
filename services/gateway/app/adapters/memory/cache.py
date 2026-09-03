"""In-memory JobCache — for tests. A dict; no TTL needed in a test run."""

from __future__ import annotations

import copy
import uuid

from app.domain.job import Job


class InMemoryJobCache:
    def __init__(self) -> None:
        self._store: dict[uuid.UUID, Job] = {}
        # test-only counters so tests can assert cache hits/misses
        self.hits = 0
        self.misses = 0

    async def get(self, job_id: uuid.UUID) -> Job | None:
        found = self._store.get(job_id)
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        return copy.deepcopy(found)

    async def set(self, job: Job) -> None:
        self._store[job.id] = copy.deepcopy(job)

    async def invalidate(self, job_id: uuid.UUID) -> None:
        self._store.pop(job_id, None)
