"""
Postgres ExposureLog. `record` is an idempotent upsert keyed on (experiment,
unit) so a unit's first-seen variant sticks and re-exposure is a no-op; `counts`
is a GROUP BY over variants.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.postgres.exposure_models import ExposureRow


class PostgresExposureLog:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def record(self, experiment_key: str, unit_id: str, variant: str) -> None:
        stmt = insert(ExposureRow).values(
            experiment_key=experiment_key, unit_id=unit_id, variant=variant,
        ).on_conflict_do_nothing(
            index_elements=[ExposureRow.experiment_key, ExposureRow.unit_id],
        )
        async with self._sm() as session:
            await session.execute(stmt)
            await session.commit()

    async def counts(self, experiment_key: str) -> dict[str, int]:
        stmt = (
            select(ExposureRow.variant, func.count())
            .where(ExposureRow.experiment_key == experiment_key)
            .group_by(ExposureRow.variant)
        )
        async with self._sm() as session:
            rows = (await session.execute(stmt)).all()
            return {variant: count for variant, count in rows}
