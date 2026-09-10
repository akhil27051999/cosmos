"""
The outbox relay — moves committed outbox rows onto the message bus.

It's the second half of the transactional outbox: writers stage rows in the DB
(atomically with their state change); this loop drains them to the bus. Ordering
matters (oldest first), and it PUBLISHES BEFORE it MARKS, so a crash in between
just re-publishes on restart — at-least-once, which is why consumers dedupe.

`relay_once` is the pure, testable unit; `run_relay` is the polling loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.ports.message_bus import MessageBus
from app.ports.outbox import OutboxRelay


async def relay_once(outbox: OutboxRelay, bus: MessageBus, batch: int = 100) -> int:
    """Publish one batch of unpublished rows; return how many were published."""
    rows = await outbox.fetch_unpublished(limit=batch)
    published: list[int] = []
    for row in rows:
        await bus.publish(row.topic, row.key, row.value)  # publish FIRST
        published.append(row.id)
    if published:
        await outbox.mark_published(published)             # mark only after success
    return len(published)


async def run_relay(
    outbox: OutboxRelay,
    bus: MessageBus,
    *,
    idle_interval: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Forever: drain the outbox; when empty, wait a beat before polling again."""
    while True:
        try:
            moved = await relay_once(outbox, bus)
        except Exception as exc:  # noqa: BLE001 - keep the relay alive on transient errors
            print(f"[relay] error: {exc!r} (will retry)")
            moved = 0
        if moved == 0:
            await sleep(idle_interval)
