"""
In-memory outbox — for tests. Implements BOTH the writer and the relay sides so
a test can stage rows, run the relay, and assert what got published.
"""

from __future__ import annotations

from app.ports.outbox import OutboxRecord


class InMemoryOutbox:
    def __init__(self) -> None:
        self._rows: list[dict] = []  # {id, topic, key, value, published}

    # writer side
    async def add(self, topic: str, key: str, value: dict) -> None:
        self._rows.append(
            {"id": len(self._rows), "topic": topic, "key": key, "value": value, "published": False}
        )

    # relay side
    async def fetch_unpublished(self, limit: int = 100) -> list[OutboxRecord]:
        pending = [r for r in self._rows if not r["published"]][:limit]
        return [OutboxRecord(id=r["id"], topic=r["topic"], key=r["key"], value=r["value"]) for r in pending]

    async def mark_published(self, ids: list[int]) -> None:
        idset = set(ids)
        for r in self._rows:
            if r["id"] in idset:
                r["published"] = True

    # --- test helpers ---
    def pending_count(self) -> int:
        return sum(1 for r in self._rows if not r["published"])

    def all_rows(self) -> list[dict]:
        return list(self._rows)
