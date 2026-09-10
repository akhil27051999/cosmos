"""
Postgres ReadModelStore — the CQRS projection, kept idempotent with an atomic
upsert.

`apply` uses `INSERT … ON CONFLICT (job_id) DO UPDATE … WHERE version < EXCLUDED.version`.
That single statement is the whole idempotency story: a first event inserts; a
newer event updates and bumps the count; a duplicate or out-of-order event
matches the conflict but the `WHERE` blocks the update — so replays are harmless.
Reads (`get`, `stats`) each use their own short session.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.postgres.read_model_models import JobReadModelRow
from app.domain.read_model import ReadModelEntry


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PostgresReadModel:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def apply(self, event: dict) -> bool:
        now = _now()
        stmt = insert(JobReadModelRow).values(
            job_id=event["job_id"], tenant_id=event.get("tenant_id", "public"),
            status=event["status"], version=int(event["version"]),
            event_count=1, created_at=now, updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[JobReadModelRow.job_id],
            set_={
                "status": stmt.excluded.status,
                "version": stmt.excluded.version,
                "event_count": JobReadModelRow.event_count + 1,
                "updated_at": now,
            },
            where=(JobReadModelRow.version < stmt.excluded.version),  # LWW guard
        )
        async with self._sm() as session:
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def get(self, job_id: str) -> ReadModelEntry | None:
        async with self._sm() as session:
            row = await session.get(JobReadModelRow, job_id)
            if row is None:
                return None
            return ReadModelEntry(
                job_id=row.job_id, status=row.status, version=row.version,
                event_count=row.event_count, created_at=row.created_at,
                updated_at=row.updated_at, tenant_id=row.tenant_id,
            )

    async def stats(self, tenant_id: str | None = None) -> dict[str, int]:
        async with self._sm() as session:
            stmt = select(JobReadModelRow.status, func.count()).group_by(JobReadModelRow.status)
            if tenant_id is not None:
                stmt = stmt.where(JobReadModelRow.tenant_id == tenant_id)
            rows = (await session.execute(stmt)).all()
            return {status: count for status, count in rows}
