"""In-memory JobDocumentStore — for tests. A dict of job_id -> JobDocument."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

from app.domain.document import JobDocument


class InMemoryDocumentStore:
    def __init__(self) -> None:
        self._docs: dict[uuid.UUID, JobDocument] = {}

    async def create(self, job_id: uuid.UUID, payload: dict) -> None:
        self._docs[job_id] = JobDocument(job_id=job_id, payload=copy.deepcopy(payload))

    async def set_result(self, job_id: uuid.UUID, result: dict) -> None:
        doc = self._docs.get(job_id)
        if doc is None:  # tolerate a missing doc the same way Mongo upsert would
            doc = JobDocument(job_id=job_id, payload={})
            self._docs[job_id] = doc
        doc.result = copy.deepcopy(result)
        doc.updated_at = datetime.now(timezone.utc)

    async def get(self, job_id: uuid.UUID) -> JobDocument | None:
        found = self._docs.get(job_id)
        return copy.deepcopy(found) if found else None
