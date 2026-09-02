"""
Postgres adapter for JobRepository.

This is the "how" behind the port. It owns two responsibilities:
  * Translating between the domain `Job` and the `JobModel` table row
    (the `_to_entity` / `_to_model` helpers).
  * Implementing each port method with real SQL via an AsyncSession.

Read `adapters/memory/repository.py` first — it does the same job with a dict
and no SQL, so it isolates the *behaviour* from the *database mechanics* you see
here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.postgres.models import JobModel
from app.domain.exceptions import JobNotFound, VersionConflict
from app.domain.job import Job, JobStatus


def _to_entity(row: JobModel) -> Job:
    """DB row -> domain object."""
    return Job(
        id=row.id,
        status=JobStatus(row.status),
        payload=row.payload,
        idempotency_key=row.idempotency_key,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_model(job: Job) -> JobModel:
    """Domain object -> DB row."""
    return JobModel(
        id=job.id,
        status=job.status.value,
        payload=job.payload,
        idempotency_key=job.idempotency_key,
        version=job.version,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


class PostgresJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        # One session per HTTP request (injected in api/deps.py). The session is
        # the "unit of work": everything done through it commits or rolls back
        # together.
        self._session = session

    async def add(self, job: Job) -> None:
        self._session.add(_to_model(job))
        try:
            await self._session.flush()  # send INSERT now so we catch conflicts
        except IntegrityError as exc:
            # The unique idempotency_key constraint fired: a concurrent request
            # created the same job first. Re-raise as a domain concept the API
            # knows how to handle (it will fetch and return the existing job).
            await self._session.rollback()
            raise VersionConflict("idempotency key already used") from exc

    async def get(self, job_id: uuid.UUID) -> Job | None:
        row = await self._session.get(JobModel, job_id)
        return _to_entity(row) if row else None

    async def get_by_idempotency_key(self, key: str) -> Job | None:
        stmt = select(JobModel).where(JobModel.idempotency_key == key)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_entity(row) if row else None

    async def list(self, limit: int = 50, offset: int = 0) -> list[Job]:
        stmt = (
            select(JobModel)
            .order_by(JobModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_entity(r) for r in rows]

    async def update(self, job: Job, expected_version: int) -> Job:
        # The heart of optimistic concurrency: a single UPDATE guarded by the
        # version the caller read. If another writer already bumped the version,
        # the WHERE clause matches zero rows and we know we lost the race.
        stmt = (
            update(JobModel)
            .where(JobModel.id == job.id, JobModel.version == expected_version)
            .values(
                status=job.status.value,
                payload=job.payload,
                version=job.version,          # already bumped by the domain
                updated_at=job.updated_at,
            )
        )
        result = await self._session.execute(stmt)

        if result.rowcount == 0:
            # Nothing updated: either the job is gone, or its version moved.
            # Distinguish the two so the client gets the right error.
            exists = await self._session.get(JobModel, job.id)
            if exists is None:
                raise JobNotFound(str(job.id))
            raise VersionConflict(
                f"expected version {expected_version}, but stored is {exists.version}"
            )
        return job
