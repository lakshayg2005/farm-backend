from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farm_common.events import Event
from logistics_service.consumers import make_handler
from logistics_service.db import (
    DeliveryPartner,
    DeliveryPool,
    PoolStatus,
    PoolStop,
    StopStatus,
    StopType,
)


def _event(user_id, roles, **extra):
    return Event(
        type="user.registered",
        producer="identity",
        payload={
            "userId": str(user_id),
            "name": "Rider Esha",
            "phone": "+919000400002",
            "roles": roles,
            **extra,
        },
    )


@pytest.mark.asyncio
async def test_delivery_users_become_partners_and_leave_when_the_role_goes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handle = make_handler(session_factory)
    uid = uuid4()
    await handle(_event(uid, ["delivery"], homeLat=21.2, homeLng=81.3))
    await handle(_event(uuid4(), ["customer"]))
    async with session_factory() as session:
        partner = await session.get(DeliveryPartner, uid)
        assert partner is not None and partner.home_lat == 21.2
    changed = Event(
        type="user.roles_changed",
        producer="identity",
        payload={"userId": str(uid), "roles": ["customer"]},
    )
    await handle(changed)
    async with session_factory() as session:
        assert await session.get(DeliveryPartner, uid) is None


def _packed(mode: str | None):
    payload = {"orderId": str(uuid4()), "deliveryDate": "2999-01-01"}
    if mode:
        payload["deliveryMode"] = mode
    return Event(type="order.ready_for_dispatch", producer="order", payload=payload)


class _Trigger:
    def __init__(self) -> None:
        self.days: list = []

    def request(self, day) -> None:
        self.days.append(day)


@pytest.mark.asyncio
async def test_a_packed_immediate_order_triggers_pooling_but_a_flexible_one_waits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    trigger = _Trigger()
    handle = make_handler(session_factory, trigger)  # type: ignore[arg-type]
    await handle(_packed("FLEXIBLE"))
    assert trigger.days == []  # held for the daily shared-delivery batch
    await handle(_packed("IMMEDIATE"))
    await handle(_packed(None))  # an event from before delivery modes existed
    assert len(trigger.days) == 2


async def _route(session_factory, order_ids, done=()):
    """One pool with a pickup and a drop for each order (the orders in `done` are delivered)."""
    pool = DeliveryPool(id=uuid4(), pool_date=date(2999, 1, 1), status=PoolStatus.ASSIGNED)
    async with session_factory() as session, session.begin():
        session.add(pool)
        await session.flush()
        for n, order_id in enumerate(order_ids):
            for offset, kind in enumerate((StopType.PICKUP, StopType.DROP)):
                session.add(
                    PoolStop(
                        pool_id=pool.id,
                        order_id=order_id,
                        stop_type=kind,
                        seq=n * 2 + offset + 1,
                        lat=21.2,
                        lng=81.3,
                        status=StopStatus.DONE if order_id in done else StopStatus.PENDING,
                    )
                )
    return pool.id


def _cancelled(order_id):
    return Event(type="order.cancelled", producer="order", payload={"orderId": str(order_id)})


async def _stops(session_factory, order_id):
    from sqlalchemy import select

    async with session_factory() as session:
        rows = await session.execute(select(PoolStop).where(PoolStop.order_id == order_id))
        return [s.status for s in rows.scalars()]


@pytest.mark.asyncio
async def test_a_cancelled_order_is_skipped_and_the_rest_of_the_route_stays(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gone, kept = uuid4(), uuid4()
    pool_id = await _route(session_factory, [gone, kept])

    await make_handler(session_factory)(_cancelled(gone))

    assert await _stops(session_factory, gone) == [StopStatus.SKIPPED] * 2
    assert await _stops(session_factory, kept) == [StopStatus.PENDING] * 2
    async with session_factory() as session:
        assert (await session.get(DeliveryPool, pool_id)).status == PoolStatus.ASSIGNED


@pytest.mark.asyncio
async def test_cancelling_the_last_open_order_closes_the_pool(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    delivered, gone = uuid4(), uuid4()
    pool_id = await _route(session_factory, [delivered, gone], done={delivered})

    await make_handler(session_factory)(_cancelled(gone))

    assert await _stops(session_factory, delivered) == [StopStatus.DONE] * 2
    async with session_factory() as session:
        assert (await session.get(DeliveryPool, pool_id)).status == PoolStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancelling_an_order_that_is_on_no_route_does_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await make_handler(session_factory)(_cancelled(uuid4()))
