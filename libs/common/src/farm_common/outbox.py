"""Transactional outbox and consumer dedupe, so events are never lost and never applied twice.

In each service's db.py:

    class OutboxEvent(OutboxMixin, Base):
        __tablename__ = "outbox"

    class ProcessedEvent(ProcessedEventMixin, Base):
        __tablename__ = "processed_events"

Publishing (inside the same transaction as the state change):

    order.status = "PLACED"
    add_event(session, OutboxEvent, Event(type="order.placed", producer="order", payload={...}))
    await session.commit()

A background task (run_relay) then publishes unsent rows to RabbitMQ.

Consuming:

    async with session.begin():
        if not await mark_processed(session, ProcessedEvent, event.event_id):
            return  # duplicate delivery, already handled
        ...apply the change...
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, String, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from farm_common.events import Event, EventPublisher

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class OutboxMixin:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    type: Mapped[str] = mapped_column(String(100), index=True)
    producer: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )

    def to_event(self) -> Event:
        return Event(
            event_id=self.id,
            type=self.type,
            producer=self.producer,
            occurred_at=self.occurred_at,
            correlation_id=self.correlation_id,
            payload=self.payload,
        )


class ProcessedEventMixin:
    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def add_event(session: AsyncSession, outbox_model: type[OutboxMixin], event: Event) -> None:
    """Stage an event in the outbox; it is saved only if the surrounding transaction commits."""
    model: Any = outbox_model  # a mapped subclass of the mixin, so it accepts column kwargs
    session.add(
        model(
            id=event.event_id,
            type=event.type,
            producer=event.producer,
            payload=event.payload,
            correlation_id=event.correlation_id,
            occurred_at=event.occurred_at,
        )
    )


async def relay_once(
    session_factory: async_sessionmaker[AsyncSession],
    outbox_model: type[OutboxMixin],
    publisher: EventPublisher,
    batch_size: int = 100,
) -> int:
    """Publish up to `batch_size` unsent events in order. Returns how many were sent.

    Rows are locked with SKIP LOCKED, so several relay instances can run safely.
    """
    model: Any = outbox_model
    async with session_factory() as session, session.begin():
        rows = (
            await session.execute(
                select(model)
                .where(model.published_at.is_(None))
                .order_by(model.occurred_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
        sent = 0
        for row in rows:
            await publisher.publish(row.to_event())
            row.published_at = _now()
            sent += 1
        return sent


async def run_relay(
    session_factory: async_sessionmaker[AsyncSession],
    outbox_model: type[OutboxMixin],
    publisher: EventPublisher,
    interval_seconds: float = 1.0,
) -> None:
    """Loop forever publishing the outbox; start it as a background task on app startup."""
    while True:
        try:
            sent = await relay_once(session_factory, outbox_model, publisher)
        except Exception:
            logger.exception("Outbox relay failed; will retry")
            sent = 0
        if sent == 0:
            await asyncio.sleep(interval_seconds)


async def mark_processed(
    session: AsyncSession, processed_model: type[ProcessedEventMixin], event_id: UUID
) -> bool:
    """Record that `event_id` was handled. Returns False if it was already handled before."""
    model: Any = processed_model
    if await session.get(model, event_id) is not None:
        return False
    try:
        async with session.begin_nested():
            session.add(model(event_id=event_id))
    except IntegrityError:
        return False
    return True
