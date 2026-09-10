"""
The MessageBus port — the event backbone (Kafka/Redpanda in prod).

Stage 5 introduces a durable, replayable event log as the platform's spine:
producers `publish` events to a **topic**; independent consumer **groups** read
them at their own pace and can replay from any offset. This is what CQRS read
models (next increment), CDC, and analytics all subscribe to.

Adapters: Redpanda (a Kafka-compatible broker) for real, and an in-memory bus
for tests. Only `publish` is exercised this increment (by the outbox relay);
`poll`/`commit` are here for the consumer side that the CQRS projector will use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Message:
    topic: str
    key: str
    value: dict
    offset: int = 0


class MessageBus(Protocol):
    async def publish(self, topic: str, key: str, value: dict) -> None:
        """Append an event to a topic (producer side — the relay)."""
        ...

    async def poll(self, group: str, topics: list[str], max_messages: int = 100) -> list[Message]:
        """Read the next batch for a consumer group (consumer side)."""
        ...

    async def commit(self, group: str) -> None:
        """Mark everything handed to this group by `poll` as processed.

        Called AFTER a batch is applied — so a crash before commit re-delivers
        the batch (at-least-once); the projector's version guard makes that safe.
        """
        ...
