"""
In-memory StreamBus — for tests and single-process use.

Each subscriber gets its own asyncio.Queue; `publish` fans an event out to every
current subscriber's queue. Works only within one process (fine for tests); the
Redis adapter is what spans the gateway and worker processes in real runs.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator


class InMemoryStreamBus:
    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, list[asyncio.Queue]] = {}

    async def publish(self, job_id: uuid.UUID, event: dict) -> None:
        for queue in list(self._subscribers.get(job_id, [])):
            await queue.put(event)

    async def subscribe(self, job_id: uuid.UUID) -> AsyncIterator[dict]:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(job_id, []).append(queue)
        try:
            while True:
                event = await queue.get()
                yield event
                if event.get("type") == "end":
                    break
        finally:
            self._subscribers.get(job_id, []).remove(queue)
