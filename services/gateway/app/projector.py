"""
The CQRS projector — consumes the event backbone and builds the read model.

It's the consumer counterpart to the outbox relay: relay writes events to the
bus, projector reads them and folds each into the read model, then commits its
offset. Because delivery is at-least-once and `ReadModelStore.apply` is
idempotent (version-guarded), re-delivered events are harmless.

`project_once` is the pure, testable unit; `run_projector` is the loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.ports.message_bus import MessageBus
from app.ports.read_model import ReadModelStore

GROUP = "cqrs-projector"
EVENTS_TOPIC = "job-events"


async def project_once(bus: MessageBus, read_model: ReadModelStore) -> int:
    """Consume one batch, fold each event into the read model, then commit."""
    messages = await bus.poll(GROUP, [EVENTS_TOPIC], max_messages=100)
    applied = 0
    for m in messages:
        if await read_model.apply(m.value):
            applied += 1
    if messages:
        # Commit AFTER applying — a crash before this re-delivers the batch,
        # which the idempotent apply tolerates (at-least-once).
        await bus.commit(GROUP)
    return applied


async def run_projector(
    bus: MessageBus,
    read_model: ReadModelStore,
    *,
    idle_interval: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    while True:
        try:
            n = await project_once(bus, read_model)
        except Exception as exc:  # noqa: BLE001 - keep the projector alive
            print(f"[projector] error: {exc!r} (will retry)")
            n = 0
        if n == 0:
            await sleep(idle_interval)
