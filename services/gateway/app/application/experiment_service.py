"""
ExperimentService — the experimentation use cases (Stage 9).

Coordinates the store (definitions) and the exposure log (measurement) over the
pure `assign()` bucketing. Its verbs: create/list/activate an experiment, resolve
a unit's assignment (recording an exposure when the experiment is live), and read
per-variant results.
"""

from __future__ import annotations

from app.domain.experiment import Assignment, Experiment, ExperimentStatus
from app.domain.exceptions import ExperimentNotFound
from app.experiments.bucketing import assign
from app.ports.experiment_store import ExperimentStore
from app.ports.exposure_log import ExposureLog


class ExperimentService:
    def __init__(self, store: ExperimentStore, exposures: ExposureLog) -> None:
        self._store = store
        self._exposures = exposures

    # ---- admin ----------------------------------------------------------

    async def create(self, experiment: Experiment) -> Experiment:
        await self._store.add(experiment)
        return experiment

    async def list(self) -> list[Experiment]:
        return await self._store.list()

    async def set_status(self, key: str, status: ExperimentStatus) -> Experiment:
        exp = await self._store.set_status(key, status)
        if exp is None:
            raise ExperimentNotFound(key)
        return exp

    # ---- assignment (the hot path) --------------------------------------

    async def assignment(self, key: str, unit_id: str, *, record: bool = True) -> Assignment:
        """Resolve `unit_id`'s variant. Records an exposure iff the experiment is
        actually running (draft/paused/completed hand out control silently, and
        we don't log control-by-default as a real exposure)."""
        exp = await self._store.get(key)
        if exp is None:
            raise ExperimentNotFound(key)
        variant = assign(exp, unit_id)
        if record and exp.is_running:
            await self._exposures.record(key, unit_id, variant.name)
        return Assignment(
            experiment_key=key, unit_id=unit_id, variant=variant.name, config=variant.config
        )

    # ---- results --------------------------------------------------------

    async def results(self, key: str) -> dict:
        exp = await self._store.get(key)
        if exp is None:
            raise ExperimentNotFound(key)
        counts = await self._exposures.counts(key)
        return {
            "experiment_key": key,
            "status": exp.status.value,
            "counts": counts,
            "total": sum(counts.values()),
        }
