"""Domain events between services, over a RabbitMQ topic exchange.

- Every event uses the same envelope (`Event`); the routing key is the event type, e.g. "order.placed".
- Publish through the outbox (farm_common.outbox), never directly from a request handler, so an
  event is never lost when the broker is down.
- Each service consumes from its own durable queue. A failing handler is retried up to
  `max_retries` times, then the message goes to the service's dead-letter queue (<queue>.dlq).
- Handlers must be idempotent: use farm_common.outbox.mark_processed() with the event_id.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractRobustConnection,
)
from pydantic import BaseModel, Field

from farm_common.log import get_correlation_id, set_correlation_id

logger = logging.getLogger(__name__)

EXCHANGE = "farm.events"
RETRY_HEADER = "x-retries"


class Event(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    type: str
    producer: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str | None = Field(default_factory=get_correlation_id)
    payload: dict[str, Any] = Field(default_factory=dict)


Handler = Callable[[Event], Awaitable[None]]


class EventPublisher(Protocol):
    async def publish(self, event: Event) -> None: ...


def topic_matches(pattern: str, routing_key: str) -> bool:
    """RabbitMQ topic matching: '*' is exactly one word, '#' is zero or more words."""

    def match(p: list[str], k: list[str]) -> bool:
        if not p:
            return not k
        if p[0] == "#":
            return any(match(p[1:], k[i:]) for i in range(len(k) + 1))
        return bool(k) and p[0] in ("*", k[0]) and match(p[1:], k[1:])

    return match(pattern.split("."), routing_key.split("."))


class RabbitEventBus:
    def __init__(self, url: str, service_name: str, max_retries: int = 3) -> None:
        self.url = url
        self.service_name = service_name
        self.max_retries = max_retries
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self.url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=20)
        self._exchange = await self._channel.declare_exchange(
            EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
        )

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()

    async def publish(self, event: Event) -> None:
        if self._exchange is None:
            raise RuntimeError("Call connect() before publishing")
        await self._exchange.publish(_to_message(event), routing_key=event.type)

    async def subscribe(
        self, binding_keys: list[str], handler: Handler, name: str = "main"
    ) -> None:
        """Consume events matching `binding_keys` (e.g. ["order.*", "quality.graded"])."""
        if self._channel is None or self._exchange is None:
            raise RuntimeError("Call connect() before subscribing")
        queue_name = f"{self.service_name}.{name}"
        await self._channel.declare_queue(f"{queue_name}.dlq", durable=True)
        queue = await self._channel.declare_queue(
            queue_name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": f"{queue_name}.dlq",
            },
        )
        for key in binding_keys:
            await queue.bind(self._exchange, routing_key=key)
        channel = self._channel

        async def on_message(message: AbstractIncomingMessage) -> None:
            event = Event.model_validate_json(message.body)
            set_correlation_id(event.correlation_id)
            try:
                await handler(event)
            except Exception:
                raw_retries = (message.headers or {}).get(RETRY_HEADER, 0)
                retries = raw_retries if isinstance(raw_retries, int) else 0
                if retries < self.max_retries:
                    logger.warning("Handler failed for %s, retry %d", event.type, retries + 1)
                    retry = _to_message(event, retries=retries + 1)
                    await channel.default_exchange.publish(retry, routing_key=queue_name)
                    await message.ack()
                else:
                    logger.exception("Handler failed for %s, sent to dead-letter queue", event.type)
                    await message.reject(requeue=False)
            else:
                await message.ack()
            finally:
                set_correlation_id(None)

        await queue.consume(on_message)


def _to_message(event: Event, retries: int = 0) -> aio_pika.Message:
    return aio_pika.Message(
        body=event.model_dump_json().encode(),
        message_id=str(event.event_id),
        type=event.type,
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        headers={RETRY_HEADER: retries, "x-correlation-id": event.correlation_id or ""},
    )


class InMemoryEventBus:
    """Drop-in replacement for tests: keeps published events and calls matching handlers."""

    def __init__(self) -> None:
        self.published: list[Event] = []
        self._subscriptions: list[tuple[list[str], Handler]] = []

    async def publish(self, event: Event) -> None:
        # round-trip through JSON so tests catch payloads that would not serialise
        event = Event.model_validate(json.loads(event.model_dump_json()))
        self.published.append(event)
        for keys, handler in self._subscriptions:
            if any(topic_matches(key, event.type) for key in keys):
                await handler(event)

    async def subscribe(
        self, binding_keys: list[str], handler: Handler, name: str = "main"
    ) -> None:
        self._subscriptions.append((binding_keys, handler))
