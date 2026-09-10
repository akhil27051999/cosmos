"""
The Outbox port — the transactional outbox pattern.

The problem it solves (flagged since Stage 1): a job's DB write and its
"publish this event" happen in two different systems (Postgres and the bus), so
a crash between them loses the event or the write — the two drift apart.

The fix: don't publish inline. Instead, in the SAME database transaction that
writes the job/event, insert a row into an `outbox` table. Because it's one
transaction, "state changed" and "event recorded for publishing" commit
together — atomically, all-or-nothing. A separate RELAY then reads unpublished
outbox rows and publishes them to the bus, marking each done. If the relay
crashes after publishing but before marking, it re-publishes on restart — so
delivery is at-least-once and consumers must be idempotent.

`OutboxWriter` is the write side (shares the caller's session). `OutboxRelay`
is the read/mark side used by the relay loop (its own session).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OutboxRecord:
    id: int
    topic: str
    key: str
    value: dict


class OutboxWriter(Protocol):
    async def add(self, topic: str, key: str, value: dict) -> None:
        """Stage an event for publishing, in the CALLER's transaction."""
        ...


class OutboxRelay(Protocol):
    async def fetch_unpublished(self, limit: int = 100) -> list[OutboxRecord]:
        """Oldest-first batch of rows not yet published."""
        ...

    async def mark_published(self, ids: list[int]) -> None:
        """Mark rows done — only after they've been published to the bus."""
        ...
