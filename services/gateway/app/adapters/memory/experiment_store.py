"""In-memory ExperimentStore — a dict keyed by experiment key."""

from __future__ import annotations

import copy

from app.domain.experiment import Experiment, ExperimentStatus


class InMemoryExperimentStore:
    def __init__(self) -> None:
        self._experiments: dict[str, Experiment] = {}

    async def add(self, experiment: Experiment) -> None:
        self._experiments[experiment.key] = copy.deepcopy(experiment)

    async def get(self, key: str) -> Experiment | None:
        found = self._experiments.get(key)
        return copy.deepcopy(found) if found else None

    async def list(self) -> list[Experiment]:
        return [copy.deepcopy(e) for e in self._experiments.values()]

    async def set_status(self, key: str, status: ExperimentStatus) -> Experiment | None:
        exp = self._experiments.get(key)
        if exp is None:
            return None
        exp.status = status
        return copy.deepcopy(exp)

    async def delete(self, key: str) -> None:
        self._experiments.pop(key, None)   # idempotent
