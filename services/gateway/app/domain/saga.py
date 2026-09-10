"""
The saga domain (Stage 11).

A **saga** is how you get "all-or-nothing" across steps that CANNOT share one
database transaction — different services, different stores, an external API call.
Instead of a distributed 2-phase commit (slow, locks, needs a coordinator), a saga
runs steps forward and, if one fails, runs each completed step's **compensation**
in reverse to undo it. The system ends either fully applied or fully unwound.

This module is the pure state: what a saga instance and its steps *are*, and the
statuses they move through. The engine (`app/saga/engine.py`) drives them; a store
persists them so a crash mid-saga can be resumed or safely compensated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SagaStatus(str, Enum):
    RUNNING = "running"            # executing steps forward
    COMPLETED = "completed"        # all steps done (success)
    COMPENSATING = "compensating"  # a step failed; undoing prior steps
    COMPENSATED = "compensated"    # fully unwound (clean failure)
    FAILED = "failed"             # a COMPENSATION failed -> needs manual repair


class StepStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    COMPENSATED = "compensated"


@dataclass
class SagaStep:
    name: str
    status: StepStatus = StepStatus.PENDING


@dataclass
class SagaInstance:
    """One run of a saga — persisted so it survives a crash.

    `context` is the shared bag steps read/write (e.g. a step stores the tenant id
    it created so a later step, or a compensation, can use it). `current_index`
    marks how far forward we've committed, which is what makes resume possible.
    """

    id: str
    name: str
    status: SagaStatus
    steps: list[SagaStep]
    context: dict
    current_index: int = 0
    error: str | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    @classmethod
    def new(cls, name: str, step_names: list[str], context: dict) -> "SagaInstance":
        return cls(
            id=uuid.uuid4().hex,
            name=name,
            status=SagaStatus.RUNNING,
            steps=[SagaStep(n) for n in step_names],
            context=dict(context),
        )

    def touch(self) -> None:
        self.updated_at = _now()
