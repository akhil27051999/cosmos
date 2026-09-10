"""
The SagaStore PORT — persists saga instances.

Persistence is what makes a saga crash-safe: after every step (and every
compensation) the engine saves the instance, so if the process dies mid-saga you
can reload it and either resume forward or finish compensating. Without this a
crash could leave the world half-applied with no record of how to unwind it.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.saga import SagaInstance


class SagaStore(Protocol):
    async def save(self, instance: SagaInstance) -> None:
        """Upsert the instance (called after every state transition)."""
        ...

    async def get(self, saga_id: str) -> SagaInstance | None:
        ...

    async def list(self) -> list[SagaInstance]:
        ...
