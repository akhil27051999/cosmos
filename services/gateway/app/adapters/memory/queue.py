"""
In-memory JobQueue — for tests.

It mimics a consumer group with explicit ack: enqueued messages sit in `_ready`;
`consume` moves them to `_pending` (claimed but unacked); `ack` drops them from
`_pending`; `reclaim_stale` re-hands out whatever is still pending. That's enough
to exercise the worker's at-least-once / idempotency behaviour without Redis.
"""

from __future__ import annotations

import uuid
from collections import deque

from app.ports.job_queue import QueueMessage


class InMemoryJobQueue:
    def __init__(self) -> None:
        self._ready: deque[QueueMessage] = deque()
        self._pending: dict[str, QueueMessage] = {}
        self._counter = 0  # deterministic message ids (no clock/randomness needed)

    async def enqueue(self, job_id: uuid.UUID) -> None:
        self._counter += 1
        self._ready.append(QueueMessage(msg_id=str(self._counter), job_id=job_id))

    async def consume(self, count: int = 10, block_ms: int = 2000) -> list[QueueMessage]:
        out: list[QueueMessage] = []
        while self._ready and len(out) < count:
            msg = self._ready.popleft()
            self._pending[msg.msg_id] = msg   # now claimed, awaiting ack
            out.append(msg)
        return out

    async def ack(self, msg_id: str) -> None:
        self._pending.pop(msg_id, None)

    async def reclaim_stale(self, min_idle_ms: int = 60_000) -> list[QueueMessage]:
        # In tests we treat everything still pending as reclaimable.
        return list(self._pending.values())

    # --- test helper (not part of the port) ---
    def depth(self) -> int:
        return len(self._ready) + len(self._pending)
