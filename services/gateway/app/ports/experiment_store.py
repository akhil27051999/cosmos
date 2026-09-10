"""
The ExperimentStore PORT — where experiment definitions live.

Tiny CRUD over `Experiment`s: create, fetch by key, list, and flip status
(draft -> running -> paused/completed). In-memory adapter for tests, Postgres for
prod, as usual.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.experiment import Experiment, ExperimentStatus


class ExperimentStore(Protocol):
    async def add(self, experiment: Experiment) -> None:
        ...

    async def get(self, key: str) -> Experiment | None:
        ...

    async def list(self) -> list[Experiment]:
        ...

    async def set_status(self, key: str, status: ExperimentStatus) -> Experiment | None:
        """Update lifecycle status; return the updated experiment, or None if absent."""
        ...
