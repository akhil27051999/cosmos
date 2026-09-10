"""
Postgres UsageLedger — an atomic upsert-increment per (tenant, period).

`record` uses `INSERT … ON CONFLICT (tenant_id, period) DO UPDATE SET used = used + :n`
and RETURNs the new total. Doing the increment inside the database means two
concurrent submissions for the same tenant can't both read "5" and both write "6"
— the classic lost-update race. The DB serialises them and the count is exact.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.postgres.usage_models import UsageRow


class PostgresUsageLedger:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def get_usage(self, tenant_id: str, period: str) -> int:
        async with self._sm() as session:
            row = await session.get(UsageRow, (tenant_id, period))
            return row.used if row else 0

    async def record(self, tenant_id: str, period: str, amount: int = 1) -> int:
        stmt = insert(UsageRow).values(tenant_id=tenant_id, period=period, used=amount)
        stmt = stmt.on_conflict_do_update(
            index_elements=[UsageRow.tenant_id, UsageRow.period],
            set_={"used": UsageRow.used + amount},
        ).returning(UsageRow.used)
        async with self._sm() as session:
            total = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return total
