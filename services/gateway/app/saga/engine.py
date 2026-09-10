"""
The saga engine (Stage 11) — runs steps forward, compensates on failure.

A `Step` bundles a forward `action` with its inverse `compensate`. A
`SagaDefinition` is an ordered list of steps. The `SagaOrchestrator`:

  * runs actions in order, persisting after each (so progress is durable);
  * on a step failure, runs the **compensations of the already-completed steps in
    REVERSE order**, persisting after each, ending COMPENSATED;
  * on success, ends COMPLETED;
  * if a *compensation* itself fails, stops and marks the saga FAILED — that's the
    one state a human must look at (the world is partially unwound).

Two correctness properties to design for:
  * **Resume / idempotency:** `run()` skips steps already `DONE`, so re-running a
    persisted instance after a crash continues where it left off. Actions and
    compensations should therefore be idempotent.
  * **Compensate in reverse:** undo in the opposite order you did, so dependencies
    are respected (you delete the key before the tenant it belongs to, etc.).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.domain.saga import SagaInstance, SagaStatus, StepStatus
from app.ports.saga_store import SagaStore

# An action reads the shared context and may return a dict merged back into it.
Action = Callable[[dict], Awaitable[dict | None]]
# A compensation reads the context and undoes the step's effect.
Compensation = Callable[[dict], Awaitable[None]]


@dataclass
class Step:
    name: str
    action: Action
    compensate: Compensation | None = None


@dataclass
class SagaDefinition:
    name: str
    steps: list[Step]

    @property
    def step_names(self) -> list[str]:
        return [s.name for s in self.steps]


class SagaOrchestrator:
    def __init__(self, store: SagaStore) -> None:
        self._store = store

    async def start(self, definition: SagaDefinition, context: dict) -> SagaInstance:
        """Create + persist a new instance, then run it to a terminal state."""
        inst = SagaInstance.new(definition.name, definition.step_names, context)
        await self._store.save(inst)
        return await self.run(definition, inst)

    async def run(self, definition: SagaDefinition, inst: SagaInstance) -> SagaInstance:
        """Drive `inst` forward. Safe to call on a resumed instance: DONE steps
        are skipped."""
        for i, step in enumerate(definition.steps):
            if inst.steps[i].status is StepStatus.DONE:
                continue  # resume: already applied
            try:
                updates = await step.action(inst.context)
                if updates:
                    inst.context.update(updates)
                inst.steps[i].status = StepStatus.DONE
                inst.current_index = i + 1
                inst.touch()
                await self._store.save(inst)
            except Exception as exc:  # noqa: BLE001 - any step failure triggers unwind
                inst.steps[i].status = StepStatus.FAILED
                inst.status = SagaStatus.COMPENSATING
                inst.error = f"step '{step.name}' failed: {exc}"
                inst.touch()
                await self._store.save(inst)
                return await self._compensate(definition, inst, failed_index=i)

        inst.status = SagaStatus.COMPLETED
        inst.touch()
        await self._store.save(inst)
        return inst

    async def _compensate(
        self, definition: SagaDefinition, inst: SagaInstance, failed_index: int
    ) -> SagaInstance:
        # Undo the completed steps BEFORE the failed one, in reverse order.
        for j in range(failed_index - 1, -1, -1):
            if inst.steps[j].status is not StepStatus.DONE:
                continue
            comp = definition.steps[j].compensate
            if comp is None:
                inst.steps[j].status = StepStatus.COMPENSATED  # nothing to undo
                continue
            try:
                await comp(inst.context)
                inst.steps[j].status = StepStatus.COMPENSATED
                inst.touch()
                await self._store.save(inst)
            except Exception as exc:  # noqa: BLE001 - compensation failed -> manual repair
                inst.status = SagaStatus.FAILED
                inst.error = f"compensation for '{definition.steps[j].name}' failed: {exc}"
                inst.touch()
                await self._store.save(inst)
                return inst

        inst.status = SagaStatus.COMPENSATED
        inst.touch()
        await self._store.save(inst)
        return inst
