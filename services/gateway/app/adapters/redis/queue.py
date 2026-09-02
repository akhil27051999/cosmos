"""
Redis Streams adapter for JobQueue.

Why Redis Streams (not a plain list)? A Stream + CONSUMER GROUP gives us exactly
the at-least-once semantics the port promises:
  * XADD appends a message (enqueue).
  * XREADGROUP hands a message to one consumer and records it as PENDING until
    it's acknowledged (consume).
  * XACK clears it from the pending list (ack).
  * XAUTOCLAIM re-assigns messages that have been pending too long — i.e. whose
    worker died before acking (reclaim_stale).

Stage 5 replaces this with Kafka for the real event backbone; the port stays
the same, so the worker and gateway won't change.
"""

from __future__ import annotations

import uuid

from redis.asyncio import Redis

from app.ports.job_queue import QueueMessage

STREAM = "cosmos:jobs"
GROUP = "workers"


class RedisJobQueue:
    def __init__(self, redis: Redis, consumer_name: str) -> None:
        self._redis = redis
        # Each worker process needs a unique name within the group so Redis can
        # track which messages *it* holds pending.
        self._consumer = consumer_name

    async def ensure_group(self) -> None:
        """Create the consumer group (and the stream) if they don't exist.

        `id="$"` means "only deliver messages added after the group is created."
        A BUSYGROUP error just means it already exists — safe to ignore.
        """
        try:
            await self._redis.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
        except Exception as exc:  # redis.exceptions.ResponseError: BUSYGROUP
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, job_id: uuid.UUID) -> None:
        await self._redis.xadd(STREAM, {"job_id": str(job_id)})

    async def consume(self, count: int = 10, block_ms: int = 2000) -> list[QueueMessage]:
        # ">" means "messages never delivered to any consumer in this group".
        resp = await self._redis.xreadgroup(
            GROUP, self._consumer, {STREAM: ">"}, count=count, block=block_ms
        )
        return self._parse(resp)

    async def ack(self, msg_id: str) -> None:
        await self._redis.xack(STREAM, GROUP, msg_id)

    async def reclaim_stale(self, min_idle_ms: int = 60_000) -> list[QueueMessage]:
        # Claim messages pending longer than min_idle_ms for THIS consumer to
        # retry. Returns (cursor, messages, deleted); we only need the messages.
        _cursor, messages, _deleted = await self._redis.xautoclaim(
            STREAM, GROUP, self._consumer, min_idle_time=min_idle_ms, start_id="0-0"
        )
        return self._parse_flat(messages)

    # --- helpers: turn Redis' nested reply shapes into QueueMessage ---

    def _parse(self, resp) -> list[QueueMessage]:
        out: list[QueueMessage] = []
        for _stream, entries in resp or []:
            out.extend(self._parse_flat(entries))
        return out

    def _parse_flat(self, entries) -> list[QueueMessage]:
        out: list[QueueMessage] = []
        for msg_id, fields in entries or []:
            job_id = fields.get("job_id") or fields.get(b"job_id")
            if isinstance(job_id, bytes):
                job_id = job_id.decode()
            if isinstance(msg_id, bytes):
                msg_id = msg_id.decode()
            out.append(QueueMessage(msg_id=msg_id, job_id=uuid.UUID(job_id)))
        return out
