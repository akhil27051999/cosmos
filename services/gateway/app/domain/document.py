"""
The JobDocument — the large, flexible part of a job.

Stage 1 stored the whole job (including its `payload`) in one Postgres row.
Stage 2 splits a job's data by SHAPE:

  * small, structured, transactional METADATA (id, status, version, timestamps)
    stays in Postgres — it needs uniqueness, ordering, and transactions.
  * the big, nested, schema-VARYING document (the input `payload` and the
    worker's `result`/trajectory) moves to MongoDB — it's document-shaped, can
    differ per job type, and never needs a relational join.

This class is that document. Pure Python, like the rest of the domain.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class JobDocument:
    job_id: uuid.UUID
    payload: dict                       # the caller's input (what to do)
    result: dict | None = None          # the worker's output (filled on success)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
