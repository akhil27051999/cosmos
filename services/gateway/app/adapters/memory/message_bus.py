"""In-memory MessageBus — for tests. Topics are lists; groups track offsets."""

from __future__ import annotations

from app.ports.message_bus import Message


class InMemoryMessageBus:
    def __init__(self) -> None:
        self.topics: dict[str, list[Message]] = {}
        self._committed: dict[tuple[str, str], int] = {}  # (group, topic) -> next offset
        self._inflight: dict[tuple[str, str], int] = {}   # advanced by poll, saved by commit

    async def publish(self, topic: str, key: str, value: dict) -> None:
        log = self.topics.setdefault(topic, [])
        log.append(Message(topic=topic, key=key, value=value, offset=len(log)))

    async def poll(self, group: str, topics: list[str], max_messages: int = 100) -> list[Message]:
        out: list[Message] = []
        for t in topics:
            start = self._committed.get((group, t), 0)
            batch = self.topics.get(t, [])[start : start + max_messages]
            if batch:
                # remember how far this poll reached; commit() saves it
                self._inflight[(group, t)] = start + len(batch)
            out.extend(batch)
        return out

    async def commit(self, group: str) -> None:
        for (g, t), offset in list(self._inflight.items()):
            if g == group:
                self._committed[(g, t)] = offset

    # --- test helper (not part of the port) ---
    def published(self, topic: str) -> list[Message]:
        return list(self.topics.get(topic, []))
