"""
The StreamBus port — live fan-out of agent steps from the worker to clients.

The agent runs in the WORKER process, but the SSE endpoint that streams its
progress lives in the GATEWAY process. So the worker `publish`es each step to a
bus, and the SSE endpoint `subscribe`s to it. Across real processes that bus is
Redis pub/sub; in tests (one process) it's an in-memory bus. Same interface.

Events are plain dicts (already JSON-friendly). A terminal event `{"type":"end"}`
tells subscribers the stream is finished.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Protocol


class StreamBus(Protocol):
    async def publish(self, job_id: uuid.UUID, event: dict) -> None:
        """Send one event to everyone currently subscribed to this job."""
        ...

    def subscribe(self, job_id: uuid.UUID) -> AsyncIterator[dict]:
        """Async-iterate a job's live events until an `{"type":"end"}` event."""
        ...
