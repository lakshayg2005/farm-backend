"""Events logistics listens to.

user.registered / user.roles_changed (identity): keep the delivery_partners table in step with
who holds the delivery role, so the pooling job has partners to assign (HLD section 14, step 5).

order.cancelled (order): an admin may cancel an order that is already on a route. Its open stops
are skipped so the partner is not sent for it, and a pool with nothing left to do is closed.

order.ready_for_dispatch (order, sent when the farmer packs): pool that delivery date's orders a
few seconds later, so partners are offered the route at once instead of at the nightly run. Only
for IMMEDIATE orders: an order the customer placed as FLEXIBLE is held for the daily
shared-delivery batch (worker.flexible_batch), where it is clustered with its neighbours.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farm_common.events import Event
from farm_common.outbox import mark_processed
from logistics_service.db import (
    DeliveryPartner,
    DeliveryPool,
    PoolStatus,
    PoolStop,
    ProcessedEvent,
    StopStatus,
)

BINDINGS = [
    "user.registered",
    "user.roles_changed",
    "order.ready_for_dispatch",
    "order.cancelled",
]

logger = logging.getLogger(__name__)


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


async def on_order_cancelled(session: AsyncSession, event: Event) -> None:
    """Skip the cancelled order's open stops; close a pool that has nothing left to do.

    A stop already DONE stays DONE. An order not on any route has no stops, so nothing happens
    (an order cancelled before pooling is simply not fetched as ready for dispatch).
    """
    raw = event.payload.get("orderId")
    if not raw:
        return
    stops = (
        (
            await session.execute(
                select(PoolStop).where(
                    PoolStop.order_id == UUID(str(raw)),
                    PoolStop.status.in_([StopStatus.PENDING, StopStatus.ARRIVED]),
                )
            )
        )
        .scalars()
        .all()
    )
    for stop in stops:
        stop.status = StopStatus.SKIPPED
        stop.notes = "Order cancelled"
    await session.flush()
    for pool_id in {stop.pool_id for stop in stops}:
        left = await session.scalar(
            select(PoolStop.id)
            .where(
                PoolStop.pool_id == pool_id,
                PoolStop.status.in_([StopStatus.PENDING, StopStatus.ARRIVED]),
            )
            .limit(1)
        )
        pool = await session.get(DeliveryPool, pool_id)
        if left is None and pool is not None:
            pool.status = PoolStatus.COMPLETED
    logger.info("Order %s cancelled: skipped %d stop(s)", raw, len(stops))


HANDLERS: dict[str, Callable[[AsyncSession, Event], Awaitable[None]]] = {
    "user.registered": on_user,
    "user.roles_changed": on_user,
    "order.cancelled": on_order_cancelled,
}


def delivery_date_of(payload: dict, today: date) -> date:
    """The day an order is pooled for: its delivery date, or today (India) when it has none
    or the date has already passed."""
    raw = payload.get("deliveryDate")
    try:
        day = date.fromisoformat(str(raw)[:10]) if raw else today
    except ValueError:
        day = today
    return max(day, today)


class PoolTrigger:
    """Runs the pooling job for a date shortly after the first packed order of a burst.

    Several orders packed within `delay_s` share one run (and so can share a pool). Runs are
    safe to overlap: run_pooling takes an advisory lock and skips orders already on a route.
    """

    def __init__(self, run: Callable[[date], Awaitable[object]], delay_s: float) -> None:
        self._run = run
        self._delay_s = delay_s
        self._pending: dict[date, asyncio.Task[None]] = {}

    def request(self, day: date) -> None:
        if day in self._pending and not self._pending[day].done():
            return
        self._pending[day] = asyncio.create_task(self._later(day))

    async def _later(self, day: date) -> None:
        await asyncio.sleep(self._delay_s)
        self._pending.pop(day, None)
        try:
            await self._run(day)
        except Exception:
            logger.exception("Pooling after a packed order failed for %s", day)


def make_handler(
    session_factory: async_sessionmaker[AsyncSession],
    trigger: PoolTrigger | None = None,
) -> Callable[[Event], Awaitable[None]]:
    async def handle(event: Event) -> None:
        if event.type == "order.ready_for_dispatch":
            if (event.payload or {}).get("deliveryMode") == "FLEXIBLE":
                logger.info(
                    "Order %s waits for the shared-delivery batch", event.payload.get("orderId")
                )
                return
            if trigger is not None:
                from logistics_service.pooling import india_today

                trigger.request(delivery_date_of(event.payload or {}, india_today()))
            return
        handler = HANDLERS.get(event.type)
        if handler is None:
            return
        async with session_factory() as session, session.begin():
            if not await mark_processed(session, ProcessedEvent, event.event_id):
                return
            await handler(session, event)

    return handle
