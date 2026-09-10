"""
The JobRepository PORT.

A "port" is an interface the domain/application defines and depends on, without
caring who implements it. This one says: "somewhere there is a thing that can
save and fetch Jobs." Whether that's Postgres, an in-memory dict, or Mongo is an
*adapter's* problem (see app/adapters/...).

Why bother? Two big payoffs you'll feel immediately:
  1. The web layer and any future worker depend on THIS abstraction, so we can
     swap the database without touching them.
  2. Tests use a fast in-memory adapter (adapters/memory) instead of a real DB —
     see tests/. Same interface, zero infrastructure.

We express the port as a `typing.Protocol` (structural typing): any class with
these methods satisfies it, no explicit inheritance required.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from app.domain.job import Job


class JobRepository(Protocol):
    async def add(self, job: Job) -> None:
        """Persist a brand-new job."""
        ...

    async def get(self, job_id: uuid.UUID, tenant_id: str | None = None) -> Job | None:
        """Return the job, or None if there is no such id.

        Stage 6 TENANT ISOLATION: when `tenant_id` is given, the row is returned
        only if it belongs to that tenant — a cross-tenant id looks exactly like
        a missing id (None), so we never even leak that another tenant's job
        exists. The API always passes the caller's tenant; the worker passes None
        (it processes jobs across all tenants as a trusted system component).
        """
        ...

    async def get_by_idempotency_key(self, key: str, tenant_id: str) -> Job | None:
        """Return the job previously created by THIS tenant under this key, if any.

        Idempotency is scoped per-tenant: two different tenants may reuse the same
        key string without colliding.
        """
        ...

    async def list(
        self, limit: int = 50, offset: int = 0, tenant_id: str | None = None
    ) -> list[Job]:
        """Return a page of jobs, newest first, scoped to `tenant_id` if given."""
        ...

    async def update(self, job: Job, expected_version: int) -> Job:
        """Persist changes to an existing job using optimistic concurrency.

        `expected_version` is the version the caller believes is current. The
        implementation MUST refuse (raise VersionConflict) if the stored version
        no longer matches — that's how concurrent writers are detected without
        locking rows.
        """
        ...
