import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from farm_common.events import Event, InMemoryEventBus, topic_matches
from farm_common.outbox import (
    OutboxMixin,
    ProcessedEventMixin,
    add_event,
    mark_processed,
    relay_once,
)


class Base(DeclarativeBase):
    pass


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedEventMixin, Base):
    __tablename__ = "processed_events"


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.parametrize(
    ("pattern", "key", "expected"),
    [
        ("order.placed", "order.placed", True),
        ("order.*", "order.placed", True),
        ("order.*", "order.payment.succeeded", False),
        ("order.#", "order.payment.succeeded", True),
        ("#", "quality.graded", True),
        ("*.graded", "quality.graded", True),
        ("land.*", "order.placed", False),
    ],
)
def test_topic_matching(pattern, key, expected):
    assert topic_matches(pattern, key) is expected


async def test_in_memory_bus_delivers_to_matching_handlers():
    bus = InMemoryEventBus()
    seen: list[str] = []

    async def handler(event: Event) -> None:
        seen.append(event.type)

    await bus.subscribe(["order.*"], handler)
    await bus.publish(Event(type="order.placed", producer="order", payload={"orderId": "o1"}))
    await bus.publish(Event(type="quality.graded", producer="quality"))

    assert seen == ["order.placed"]
    assert [e.type for e in bus.published] == ["order.placed", "quality.graded"]


async def test_outbox_relays_each_event_once_in_order(session_factory):
    bus = InMemoryEventBus()
    first = Event(type="order.placed", producer="order", payload={"orderId": "o1"})
    second = Event(type="order.cancelled", producer="order", payload={"orderId": "o1"})
    async with session_factory() as session, session.begin():
        add_event(session, OutboxEvent, first)
        add_event(session, OutboxEvent, second)

    assert await relay_once(session_factory, OutboxEvent, bus) == 2
    assert await relay_once(session_factory, OutboxEvent, bus) == 0
    assert [e.event_id for e in bus.published] == [first.event_id, second.event_id]
    assert bus.published[0].payload == {"orderId": "o1"}


async def test_outbox_event_is_dropped_when_transaction_rolls_back(session_factory):
    bus = InMemoryEventBus()
    async with session_factory() as session:
        add_event(session, OutboxEvent, Event(type="order.placed", producer="order"))
        await session.rollback()

    assert await relay_once(session_factory, OutboxEvent, bus) == 0


async def test_mark_processed_detects_duplicates(session_factory):
    event = Event(type="order.placed", producer="order")
    async with session_factory() as session, session.begin():
        assert await mark_processed(session, ProcessedEvent, event.event_id) is True
    async with session_factory() as session, session.begin():
        assert await mark_processed(session, ProcessedEvent, event.event_id) is False
