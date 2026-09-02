"""Postgres EventStore — inserts rows into job_events, reads them back in order."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.postgres.event_models import JobEventModel
from app.domain.events import JobEvent, JobEventType


def _to_entity(row: JobEventModel) -> JobEvent:
    return JobEvent(
        id=row.id,
        job_id=row.job_id,
        type=JobEventType(row.type),
        data=row.data,
        at=row.at,
    )


class PostgresEventStore:
    def __init__(self, session: AsyncSession) -> None:
        # Shares the request's / worker's session, so appending an event and
        # updating the job commit together in one transaction.
        self._session = session

    async def append(self, event: JobEvent) -> None:
        self._session.add(
            JobEventModel(
                id=event.id,
                job_id=event.job_id,
                type=event.type.value,
                data=event.data,
                at=event.at,
            )
        )
        await self._session.flush()

    async def list_for_job(self, job_id: uuid.UUID) -> list[JobEvent]:
        stmt = (
            select(JobEventModel)
            .where(JobEventModel.job_id == job_id)
            .order_by(JobEventModel.seq.asc())   # stable total order
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_entity(r) for r in rows]
