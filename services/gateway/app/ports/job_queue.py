"""
The JobQueue port — the durable hand-off between the gateway and the worker.

The gateway ENQUEUES a job id and returns immediately; a worker later CONSUMES
it and does the work. That decoupling is the whole "accept fast, process later"
idea (202 + id).

The interface is shaped around a CONSUMER GROUP with explicit acknowledgement,
which is what gives us AT-LEAST-ONCE delivery:
  * `consume` hands out messages but keeps them "pending" until acked.
  * `ack` confirms a message was fully processed; only then is it forgotten.
  * if a worker dies before `ack`, the message is still pending and
    `reclaim_stale` can hand it to another worker — so no job is silently lost.

At-least-once means a message MAY be delivered more than once (e.g. after a
crash), so the worker must be IDEMPOTENT. See app/worker.py.

Adapters: Redis Streams (real) and in-memory (tests).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class QueueMessage:
    """A delivery: the queue's own message id plus the job it points at.

    `msg_id` is the queue's handle (used to ack); `job_id` is our payload.
    """

    msg_id: str
    job_id: uuid.UUID


class JobQueue(Protocol):
    async def enqueue(self, job_id: uuid.UUID) -> None:
        """Append a job id to the queue (producer side — the gateway)."""
        ...

    async def consume(self, count: int = 10, block_ms: int = 2000) -> list[QueueMessage]:
        """Claim up to `count` new messages for this consumer, or wait up to
        `block_ms` for one to arrive. Claimed messages stay pending until acked.
        """
        ...

    async def ack(self, msg_id: str) -> None:
        """Confirm a message is fully handled; the queue may forget it now."""
        ...

    async def reclaim_stale(self, min_idle_ms: int = 60_000) -> list[QueueMessage]:
        """Re-deliver messages that were claimed but never acked for longer than
        `min_idle_ms` — i.e. their worker probably crashed. This is what makes
        delivery at-least-once rather than at-most-once.
        """
        ...
