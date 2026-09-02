"""
The EventStore port — where domain events are appended and read.

Append-only by design: there is deliberately no update or delete. An event
store only ever grows. Adapters: Postgres (real) and in-memory (tests).
"""

from __future__ import annotations

import uuid
from typing import Protocol

from app.domain.events import JobEvent


class EventStore(Protocol):
    async def append(self, event: JobEvent) -> None:
        """Add one event to the log. Never modifies existing events."""
        ...

    async def list_for_job(self, job_id: uuid.UUID) -> list[JobEvent]:
        """Return a job's events in the order they happened (oldest first)."""
        ...
