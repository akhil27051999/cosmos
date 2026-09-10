"""In-memory ReadModelStore — for tests. A dict keyed by job_id, LWW by version."""

from __future__ import annotations

import copy
from collections import Counter
from datetime import datetime, timezone

from app.domain.read_model import ReadModelEntry


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryReadModel:
    def __init__(self) -> None:
        self._entries: dict[str, ReadModelEntry] = {}

    async def apply(self, event: dict) -> bool:
        job_id = event["job_id"]
        version = int(event["version"])
        status = event["status"]
        tenant_id = event.get("tenant_id", "public")

        entry = self._entries.get(job_id)
        if entry is None:
            self._entries[job_id] = ReadModelEntry(
                job_id=job_id, status=status, version=version,
                event_count=1, created_at=_now(), updated_at=_now(),
                tenant_id=tenant_id,
            )
            return True
        if version > entry.version:                 # newer -> advance
            entry.status = status
            entry.version = version
            entry.event_count += 1
            entry.updated_at = _now()
            return True
        return False                                # duplicate/stale -> idempotent no-op

    async def get(self, job_id: str) -> ReadModelEntry | None:
        found = self._entries.get(job_id)
        return copy.deepcopy(found) if found else None

    async def stats(self, tenant_id: str | None = None) -> dict[str, int]:
        entries = self._entries.values()
        if tenant_id is not None:
            entries = [e for e in entries if e.tenant_id == tenant_id]
        return dict(Counter(e.status for e in entries))
