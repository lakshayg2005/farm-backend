from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farm_common.events import Event
from logistics_service.consumers import make_handler
from logistics_service.db import DeliveryPartner


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
