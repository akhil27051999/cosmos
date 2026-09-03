"""
MongoDB adapter for JobDocumentStore (via `motor`, the async Mongo driver).

Modelling choice: one document per job, keyed by the job id as `_id`, holding
`payload` and `result` inline. This is the classic Mongo "embed, don't join"
approach — everything about a job's data is one document you fetch in one round
trip, exactly the shape a document DB is good at.

We store the job id (a UUID) as a string `_id`. `set_result` uses `upsert=True`
so it's safe even if the create somehow didn't land.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from app.domain.document import JobDocument

COLLECTION = "job_documents"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MongoDocumentStore:
    def __init__(self, client: AsyncIOMotorClient, db_name: str) -> None:
        self._col = client[db_name][COLLECTION]

    async def create(self, job_id: uuid.UUID, payload: dict) -> None:
        now = _now()
        await self._col.insert_one(
            {
                "_id": str(job_id),
                "payload": payload,
                "result": None,
                "created_at": now,
                "updated_at": now,
            }
        )

    async def set_result(self, job_id: uuid.UUID, result: dict) -> None:
        await self._col.update_one(
            {"_id": str(job_id)},
            {"$set": {"result": result, "updated_at": _now()}},
            upsert=True,
        )

    async def get(self, job_id: uuid.UUID) -> JobDocument | None:
        doc = await self._col.find_one({"_id": str(job_id)})
        if doc is None:
            return None
        return JobDocument(
            job_id=job_id,
            payload=doc.get("payload", {}),
            result=doc.get("result"),
            created_at=doc.get("created_at", _now()),
            updated_at=doc.get("updated_at", _now()),
        )
