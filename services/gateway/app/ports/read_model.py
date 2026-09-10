"""
The ReadModelStore port — the CQRS read side.

`apply` folds one job-event into the view and MUST be idempotent (at-least-once
delivery means it may see the same event twice): it updates only when the
event's version is newer than what's stored. `get` and `stats` are the fast
read queries the API serves from here instead of touching the write model.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.read_model import ReadModelEntry


class ReadModelStore(Protocol):
    async def apply(self, event: dict) -> bool:
        """Fold a job-event ({type, job_id, status, version}) into the view.

        Returns True if it advanced the view, False if it was a stale/duplicate
        event (idempotent no-op).
        """
        ...

    async def get(self, job_id: str) -> ReadModelEntry | None:
        ...

    async def stats(self, tenant_id: str | None = None) -> dict[str, int]:
        """Count of jobs per status — a query the write model would need a
        GROUP BY for, served instantly from the projection. Scoped to
        `tenant_id` when given (the API always passes the caller's tenant)."""
        ...
