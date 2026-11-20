"""Events logistics listens to.

user.registered / user.roles_changed (identity): keep the delivery_partners table in step with
who holds the delivery role, so the pooling job has partners to assign (HLD section 14, step 5).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farm_common.events import Event
from farm_common.outbox import mark_processed
from logistics_service.db import DeliveryPartner, ProcessedEvent

BINDINGS = ["user.registered", "user.roles_changed"]


async def on_user(session: AsyncSession, event: Event) -> None:
    payload = event.payload
    raw_id = payload.get("userId") or payload.get("id")
    if not raw_id:
        return
    user_id = UUID(str(raw_id))
    partner = await session.get(DeliveryPartner, user_id)
    is_partner = "delivery" in (payload.get("roles") or [])
    if not is_partner:
        if partner is not None:
            await session.delete(partner)
        return
    if partner is None:
        partner = DeliveryPartner(
            id=user_id, name=str(payload.get("name") or "Partner"), is_online=True
        )
        session.add(partner)
    partner.name = str(payload.get("name") or partner.name)
    partner.phone = payload.get("phone")
    lat, lng = payload.get("homeLat"), payload.get("homeLng")
    partner.home_lat = float(lat) if lat is not None else None
    partner.home_lng = float(lng) if lng is not None else None


HANDLERS: dict[str, Callable[[AsyncSession, Event], Awaitable[None]]] = {
    "user.registered": on_user,
    "user.roles_changed": on_user,
}


def make_handler(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[Event], Awaitable[None]]:
    async def handle(event: Event) -> None:
        handler = HANDLERS.get(event.type)
        if handler is None:
            return
        async with session_factory() as session, session.begin():
            if not await mark_processed(session, ProcessedEvent, event.event_id):
                return
            await handler(session, event)

    return handle
