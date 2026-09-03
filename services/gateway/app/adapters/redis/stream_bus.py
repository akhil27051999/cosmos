"""
Redis pub/sub adapter for StreamBus — spans processes.

The worker `publish`es agent steps to a per-job channel; the gateway's SSE
endpoint `subscribe`s to that channel and forwards them to the browser. Pub/sub
is *live-only* — a subscriber sees events published after it subscribes (a
production version would first replay the stored trajectory from Mongo, then go
live; noted as a Stage-3 gap).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from redis.asyncio import Redis


def _channel(job_id: uuid.UUID) -> str:
    return f"job:{job_id}:stream"


class RedisStreamBus:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, job_id: uuid.UUID, event: dict) -> None:
        await self._redis.publish(_channel(job_id), json.dumps(event))

    async def subscribe(self, job_id: uuid.UUID) -> AsyncIterator[dict]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_channel(job_id))
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue  # skip subscribe/unsubscribe acks
                event = json.loads(message["data"])
                yield event
                if event.get("type") == "end":
                    break
        finally:
            await pubsub.unsubscribe(_channel(job_id))
            await pubsub.aclose()
