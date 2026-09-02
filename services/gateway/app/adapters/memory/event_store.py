"""In-memory EventStore — for tests. Append-only dict of job_id -> [events]."""

from __future__ import annotations

import uuid

from app.domain.events import JobEvent


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[uuid.UUID, list[JobEvent]] = {}

    async def append(self, event: JobEvent) -> None:
        self._events.setdefault(event.job_id, []).append(event)

    async def list_for_job(self, job_id: uuid.UUID) -> list[JobEvent]:
        # events are frozen, so returning the list directly is safe enough
        return list(self._events.get(job_id, []))
