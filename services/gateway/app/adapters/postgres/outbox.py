"""
Postgres outbox adapters.

`PostgresOutbox` (writer) shares the caller's session, so the outbox INSERT is
part of the same transaction as the job/event write — the whole point of the
pattern.

`PostgresOutboxRelay` (relay side) owns its own short session per poll: read the
oldest unpublished rows, and stamp `published_at` once the relay has pushed them
to the bus.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.postgres.outbox_models import OutboxModel
from app.ports.outbox import OutboxRecord


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PostgresOutbox:
    """Writer — uses the caller's session (atomic with their state change)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, topic: str, key: str, value: dict) -> None:
        self._session.add(
            OutboxModel(topic=topic, key=key, value=value, created_at=_now(), published_at=None)
        )
        await self._session.flush()


class PostgresOutboxRelay:
    """Relay side — its own session per operation."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def fetch_unpublished(self, limit: int = 100) -> list[OutboxRecord]:
        async with self._sm() as session:
            stmt = (
                select(OutboxModel)
                .where(OutboxModel.published_at.is_(None))
                .order_by(OutboxModel.seq.asc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [OutboxRecord(id=r.seq, topic=r.topic, key=r.key, value=r.value) for r in rows]

    async def mark_published(self, ids: list[int]) -> None:
        if not ids:
            return
        async with self._sm() as session:
            await session.execute(
                update(OutboxModel).where(OutboxModel.seq.in_(ids)).values(published_at=_now())
            )
            await session.commit()
