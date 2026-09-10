"""In-memory SagaStore — a dict of deep-copied instances."""

from __future__ import annotations

import copy

from app.domain.saga import SagaInstance


class InMemorySagaStore:
    def __init__(self) -> None:
        self._sagas: dict[str, SagaInstance] = {}

    async def save(self, instance: SagaInstance) -> None:
        self._sagas[instance.id] = copy.deepcopy(instance)

    async def get(self, saga_id: str) -> SagaInstance | None:
        found = self._sagas.get(saga_id)
        return copy.deepcopy(found) if found else None

    async def list(self) -> list[SagaInstance]:
        return [copy.deepcopy(s) for s in self._sagas.values()]
