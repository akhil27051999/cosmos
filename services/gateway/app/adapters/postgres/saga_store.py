"""Postgres SagaStore — upsert the whole instance as one row (own sessions)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.postgres.saga_models import SagaModel
from app.domain.saga import SagaInstance, SagaStatus, SagaStep, StepStatus


def _to_entity(row: SagaModel) -> SagaInstance:
    return SagaInstance(
        id=row.id, name=row.name, status=SagaStatus(row.status),
        steps=[SagaStep(s["name"], StepStatus(s["status"])) for s in row.steps],
        context=row.context, current_index=row.current_index, error=row.error,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _steps_json(inst: SagaInstance) -> list:
    return [{"name": s.name, "status": s.status.value} for s in inst.steps]


class PostgresSagaStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def save(self, instance: SagaInstance) -> None:
        async with self._sm() as session:
            row = await session.get(SagaModel, instance.id)
            if row is None:
                session.add(SagaModel(
                    id=instance.id, name=instance.name, status=instance.status.value,
                    steps=_steps_json(instance), context=instance.context,
                    current_index=instance.current_index, error=instance.error,
                    created_at=instance.created_at, updated_at=instance.updated_at,
                ))
            else:
                row.status = instance.status.value
                row.steps = _steps_json(instance)
                row.context = instance.context
                row.current_index = instance.current_index
                row.error = instance.error
                row.updated_at = instance.updated_at
            await session.commit()

    async def get(self, saga_id: str) -> SagaInstance | None:
        async with self._sm() as session:
            row = await session.get(SagaModel, saga_id)
            return _to_entity(row) if row else None

    async def list(self) -> list[SagaInstance]:
        async with self._sm() as session:
            rows = (await session.execute(select(SagaModel))).scalars().all()
            return [_to_entity(r) for r in rows]
