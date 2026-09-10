"""Postgres ExperimentStore — CRUD over the `experiments` table (own sessions)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.postgres.experiment_models import ExperimentModel
from app.domain.experiment import Experiment, ExperimentStatus, Variant


def _to_entity(row: ExperimentModel) -> Experiment:
    return Experiment(
        key=row.key,
        description=row.description,
        status=ExperimentStatus(row.status),
        variants=[Variant(name=v["name"], weight=v["weight"], config=v.get("config", {}))
                  for v in row.variants],
        created_at=row.created_at,
    )


def _variants_json(exp: Experiment) -> list:
    return [{"name": v.name, "weight": v.weight, "config": v.config} for v in exp.variants]


class PostgresExperimentStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def add(self, experiment: Experiment) -> None:
        async with self._sm() as session:
            session.add(ExperimentModel(
                key=experiment.key, description=experiment.description,
                status=experiment.status.value, variants=_variants_json(experiment),
                created_at=experiment.created_at,
            ))
            await session.commit()

    async def get(self, key: str) -> Experiment | None:
        async with self._sm() as session:
            row = await session.get(ExperimentModel, key)
            return _to_entity(row) if row else None

    async def list(self) -> list[Experiment]:
        async with self._sm() as session:
            rows = (await session.execute(select(ExperimentModel))).scalars().all()
            return [_to_entity(r) for r in rows]

    async def set_status(self, key: str, status: ExperimentStatus) -> Experiment | None:
        async with self._sm() as session:
            row = await session.get(ExperimentModel, key)
            if row is None:
                return None
            row.status = status.value
            await session.commit()
            return _to_entity(row)

    async def delete(self, key: str) -> None:
        async with self._sm() as session:
            row = await session.get(ExperimentModel, key)
            if row is not None:
                await session.delete(row)
                await session.commit()
