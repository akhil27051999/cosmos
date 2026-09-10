"""
The CQRS read model entry — a denormalized, read-optimized view of a job.

CQRS = Command Query Responsibility Segregation: the WRITE model (the jobs +
job_events tables, optimized for correctness/transactions) is separate from the
READ model (this — optimized for fast queries). The read model is built by
projecting the event stream, so it's eventually consistent with the write side.

`version` is the job's version at the projected event; the projector uses it as
a last-writer-wins guard so duplicate/out-of-order events (at-least-once
delivery) can't corrupt the view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ReadModelEntry:
    job_id: str
    status: str
    version: int
    event_count: int
    created_at: datetime
    updated_at: datetime
    # Stage 6: the read side is tenant-scoped too — the event carries the job's
    # tenant, so /stats and /view can be filtered per tenant (no cross-tenant leak).
    tenant_id: str = "public"
