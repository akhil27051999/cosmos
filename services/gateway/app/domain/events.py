"""
Domain events — the heart of EVENT SOURCING.

Stage 0 stored only the job's *current* state. Stage 1 adds an append-only log
of everything that ever happened to a job: it was submitted, it started, it
succeeded/failed/was cancelled. Each of these is an immutable `JobEvent`.

Why keep a log when we already have the current status?
  * Audit & debugging — you can see the full history, in order, with timestamps.
  * Replay — the current state is derivable from the events (state = fold(events)).
  * Foundation — this log is what CQRS read models and CDC (Stage 5) build on.

Like `job.py`, this is pure Python: no DB, no framework.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class JobEventType(str, Enum):
    SUBMITTED = "submitted"    # job accepted onto the queue
    STARTED = "started"        # a worker began processing it
    SUCCEEDED = "succeeded"    # work finished OK
    FAILED = "failed"          # work raised an error
    CANCELLED = "cancelled"    # stopped on request


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class JobEvent:
    """One immutable fact about a job.

    `frozen=True` makes instances read-only — you never edit an event, you only
    append new ones. That immutability is what makes the log trustworthy.
    """

    job_id: uuid.UUID
    type: JobEventType
    # free-form extra context, e.g. {"error": "..."} on a FAILED event
    data: dict = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    at: datetime = field(default_factory=_now)
