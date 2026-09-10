"""
Redpanda (Kafka-compatible) MessageBus adapter, via `aiokafka`.

Redpanda speaks the Kafka protocol, so this is a normal Kafka producer. Values
are JSON-encoded; the key (job id) determines the partition, which keeps all
events for one job in order on the same partition — important for the CQRS
projector later.

`publish` is what the outbox relay uses this increment. `poll`/`commit` (the
consumer group) land with the CQRS projector in the next increment.
"""

from __future__ import annotations

import json

from app.ports.message_bus import Message


class RedpandaMessageBus:
    def __init__(self, brokers: str) -> None:
        self._brokers = brokers
        self._producer = None                 # lazy producer
        self._consumers: dict[str, object] = {}  # one AIOKafkaConsumer per group

    async def _get_producer(self):
        if self._producer is None:
            from aiokafka import AIOKafkaProducer  # lazy: keep aiokafka out of test imports

            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._brokers,
                value_serializer=lambda v: json.dumps(v).encode(),
                key_serializer=lambda k: k.encode(),
                enable_idempotence=True,   # no duplicate/re-ordered producer writes
            )
            await self._producer.start()
        return self._producer

    async def publish(self, topic: str, key: str, value: dict) -> None:
        producer = await self._get_producer()
        await producer.send_and_wait(topic, value=value, key=key)

    async def _get_consumer(self, group: str, topics: list[str]):
        if group not in self._consumers:
            from aiokafka import AIOKafkaConsumer

            consumer = AIOKafkaConsumer(
                *topics,
                bootstrap_servers=self._brokers,
                group_id=group,
                enable_auto_commit=False,          # we commit explicitly, after applying
                auto_offset_reset="earliest",      # a new group reads from the start
                value_deserializer=lambda b: json.loads(b.decode()),
                key_deserializer=lambda b: b.decode() if b else "",
            )
            await consumer.start()
            self._consumers[group] = consumer
        return self._consumers[group]

    async def poll(self, group: str, topics: list[str], max_messages: int = 100) -> list[Message]:
        consumer = await self._get_consumer(group, topics)
        batches = await consumer.getmany(timeout_ms=1000, max_records=max_messages)
        out: list[Message] = []
        for tp, records in batches.items():
            for r in records:
                out.append(Message(topic=tp.topic, key=r.key or "", value=r.value, offset=r.offset))
        return out

    async def commit(self, group: str) -> None:
        consumer = self._consumers.get(group)
        if consumer is not None:
            await consumer.commit()  # commit the consumer's current positions
