"""
The ExposureLog PORT — records which unit saw which variant.

An A/B test is only measurable if you log **exposures** (a unit was actually
bucketed into a variant). We key exposures by (experiment, unit) so re-exposing
the same unit is idempotent — `counts()` therefore reports *unique units per
variant*, the denominator you'd compare outcome metrics against.
"""

from __future__ import annotations

from typing import Protocol


class ExposureLog(Protocol):
    async def record(self, experiment_key: str, unit_id: str, variant: str) -> None:
        """Record (idempotently) that `unit_id` was exposed to `variant`."""
        ...

    async def counts(self, experiment_key: str) -> dict[str, int]:
        """Unique units per variant, e.g. {"control": 40, "variant-a": 38}."""
        ...
