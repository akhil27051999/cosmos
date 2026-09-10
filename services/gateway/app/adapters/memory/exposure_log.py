"""In-memory ExposureLog — unique (experiment, unit) -> variant, so counts are
per-variant unique units."""

from __future__ import annotations

from collections import Counter


class InMemoryExposureLog:
    def __init__(self) -> None:
        # experiment_key -> {unit_id: variant}
        self._seen: dict[str, dict[str, str]] = {}

    async def record(self, experiment_key: str, unit_id: str, variant: str) -> None:
        units = self._seen.setdefault(experiment_key, {})
        units.setdefault(unit_id, variant)   # first exposure sticks (idempotent)

    async def counts(self, experiment_key: str) -> dict[str, int]:
        units = self._seen.get(experiment_key, {})
        return dict(Counter(units.values()))
