"""
The Job entity — the HEART of the domain.

Read this file first. It is deliberately *pure Python*: no FastAPI, no
SQLAlchemy, no database, no HTTP. It only knows the business rules of what a
"job" is and how it may legally change. That purity is the point of hexagonal
(ports-and-adapters) architecture — the rules live in the centre and never
depend on the frameworks around them, so they are trivial to test and reason
about.

Concepts demonstrated here:
  * A domain entity (identity + state + behaviour).
  * A finite STATE MACHINE with explicit, guarded transitions.
  * A `version` field that powers OPTIMISTIC CONCURRENCY CONTROL (OCC) later.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum

from app.domain.exceptions import InvalidTransition


class JobStatus(str, Enum):
    """The states a job can be in.

    Inheriting from `str` means the value serialises straight to "pending" etc.
    in JSON and in the DB, while still being a real enum in code.
    """

    PENDING = "pending"      # accepted, not yet picked up by a worker
    RUNNING = "running"      # a worker is processing it
    SUCCEEDED = "succeeded"  # finished OK  (terminal)
    FAILED = "failed"        # finished with an error (terminal)
    CANCELLED = "cancelled"  # stopped on request (terminal)


# The state machine, expressed as data: "from -> the set of allowed to-states".
# Terminal states map to the empty set — nothing may leave them. Stage 1 will
# grow this; keeping it explicit from day one is what stops illegal states.
_ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


def _now() -> datetime:
    """UTC, timezone-aware. Always store UTC; format for humans at the edges."""
    return datetime.now(timezone.utc)


@dataclass
class Job:
    """A unit of work submitted to the platform.

    It is a `@dataclass` (a plain data holder) plus a couple of methods that
    enforce the rules. Nothing in here reaches out to a database — persistence
    is somebody else's job (see `ports/job_repository.py`).
    """

    # NB: the job's `payload` used to live here (Stage 1). In Stage 2 it moved
    # to MongoDB (see domain/document.py) because it's large and schema-varying;
    # the Job entity now holds only small, transactional METADATA.
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    # Stage 6: every job belongs to a TENANT. This is the isolation key — the
    # repository scopes reads/writes by it, so one tenant can never see another's
    # jobs. Defaulted so pre-Stage-6 code paths (and tests) still construct a Job,
    # but the submit path always stamps the authenticated tenant.
    tenant_id: str = "public"
    status: JobStatus = JobStatus.PENDING
    # The idempotency key ties a job to the client's "please do this exactly
    # once" request. Two submits with the same key must yield the same job.
    idempotency_key: str | None = None
    # `version` increments on every change. The repository compares the version
    # it loaded against the version in the DB on write; a mismatch means someone
    # else changed the row first — that is OPTIMISTIC CONCURRENCY CONTROL.
    version: int = 1
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    # ----- behaviour (the rules) ------------------------------------------

    def can_transition_to(self, target: JobStatus) -> bool:
        return target in _ALLOWED_TRANSITIONS[self.status]

    def transition_to(self, target: JobStatus) -> None:
        """Move to a new status IF the move is legal, else refuse.

        Mutating in place is fine for an entity; the guard is what matters.
        We bump `version` and `updated_at` so the change is detectable and
        auditable. An illegal move raises a *domain* error — the web layer
        turns that into an HTTP 409 later, but the domain doesn't know or care
        about HTTP.
        """
        if not self.can_transition_to(target):
            raise InvalidTransition(
                f"cannot move a job from '{self.status.value}' to '{target.value}'"
            )
        self.status = target
        self.version += 1
        self.updated_at = _now()

    @property
    def is_terminal(self) -> bool:
        return not _ALLOWED_TRANSITIONS[self.status]
