"""
Redis adapter for JobCache.

Stores a job's metadata as JSON under `job:{id}` with a short TTL, so a burst of
polls is served from Redis instead of Postgres. The TTL is a safety net: even if
an invalidation is somehow missed, a stale entry self-expires quickly.

The port speaks in domain `Job` objects, so this adapter owns the (de)serialize.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from redis.asyncio import Redis

from app.domain.job import Job, JobStatus

DEFAULT_TTL_SECONDS = 30


class RedisJobCache:
    def __init__(self, redis: Redis, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(job_id: uuid.UUID) -> str:
        return f"job:{job_id}"

    async def get(self, job_id: uuid.UUID) -> Job | None:
        raw = await self._redis.get(self._key(job_id))
        if not raw:
            return None
        return _deserialize(raw)

    async def set(self, job: Job) -> None:
        # `ex` sets the TTL in seconds — the entry auto-expires.
        await self._redis.set(self._key(job.id), _serialize(job), ex=self._ttl)

    async def invalidate(self, job_id: uuid.UUID) -> None:
        await self._redis.delete(self._key(job_id))


def _serialize(job: Job) -> str:
    return json.dumps(
        {
            "id": str(job.id),
            "status": job.status.value,
            "idempotency_key": job.idempotency_key,
            "version": job.version,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }
    )


def _deserialize(raw: str) -> Job:
    d = json.loads(raw)
    return Job(
        id=uuid.UUID(d["id"]),
        status=JobStatus(d["status"]),
        idempotency_key=d["idempotency_key"],
        version=d["version"],
        created_at=datetime.fromisoformat(d["created_at"]),
        updated_at=datetime.fromisoformat(d["updated_at"]),
    )
