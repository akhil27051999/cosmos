"""
The JobDocumentStore port — where a job's big document lives (payload + result).

Adapters: MongoDB (real) and in-memory (tests). The application depends on this
interface, so "the payload lives in Mongo" is an adapter detail, not something
the routes/worker/service know about.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from app.domain.document import JobDocument


class JobDocumentStore(Protocol):
    async def create(self, job_id: uuid.UUID, payload: dict) -> None:
        """Store a new job's input payload."""
        ...

    async def set_result(self, job_id: uuid.UUID, result: dict) -> None:
        """Attach the worker's result to an existing document."""
        ...

    async def get(self, job_id: uuid.UUID) -> JobDocument | None:
        """Fetch the full document (payload + result), or None."""
        ...
