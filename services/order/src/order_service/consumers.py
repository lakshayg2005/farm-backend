"""Handlers of the events the order service consumes (see events.CONSUMED)."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farm_common.events import Event
from farm_common.outbox import mark_processed
from order_service import events as ev
from order_service import harvest
from order_service.db import OrderRow, ProcessedEvent
from order_service.payments import MockPaymentGateway, PaymentGateway
from order_service.schemas import OrderStatus
from order_service.service import _LOAD, _pick, _transition, _uuid, cancel_row
from order_service.settings import OrderSettings, get_order_settings

logger = logging.getLogger(__name__)

S = OrderStatus


def _order_ids(payload: dict[str, Any]) -> list[UUID]:
    raw = _pick(payload, "orderIds", "order_ids") or [_pick(payload, "orderId", "order_id")]
    ids = []
    for value in raw:
        try:
            ids.append(UUID(str(value)))
        except ValueError:
            logger.warning("Ignoring invalid order id %r in event payload", value)
    return ids


async def _load(session: AsyncSession, order_id: UUID) -> OrderRow | None:
    return (
        await session.execute(select(OrderRow).where(OrderRow.id == order_id).options(*_LOAD))
    ).scalar_one_or_none()


async def _advance(
    session: AsyncSession,
    order_id: UUID,
    expected: set[OrderStatus],
    steps: list[tuple[OrderStatus, str | None]],
    note: str,
) -> None:
    """Apply `steps` to an order that is currently in one of `expected`; skip otherwise."""
    order = await _load(session, order_id)
    if order is None or OrderStatus(order.status) not in expected:
        logger.info(
            "Skipping %s for order %s in status %s", note, order_id, order.status if order else None
        )
        return
    for new, event_type in steps:
        await _transition(session, order, new, event_type, note=note)


async def _order_graded(session: AsyncSession, payload: dict[str, Any]) -> None:
    """quality.graded for a packed produce order: PASS makes it ready for dispatch, FAIL cancels it
    and refunds it (TODO(M1): assumed policy; the farmer could be asked to re-pack instead)."""
    order_id = _uuid(payload, "orderId", "order_id", "subjectId", "subject_id")
    if order_id is None:
        return
    if str(_pick(payload, "result") or "PASS").upper() == "FAIL":
        order = await _load(session, order_id)
        if order is None or order.status != S.PACKED:
            logger.info("Skipping failed inspection of order %s", order_id)
            return
        await cancel_row(session, order, "system", "QUALITY_FAILED")
        return
    await _advance(
        session,
        order_id,
        {S.PACKED},
        [(S.QC_PASSED, None), (S.READY_FOR_DISPATCH, ev.ORDER_READY_FOR_DISPATCH)],
        "Quality check passed",
    )


async def handle_event(
    session_factory: async_sessionmaker[AsyncSession],
    event: Event,
    settings: OrderSettings | None = None,
    gateway: PaymentGateway | None = None,
) -> None:
    """Apply an event from another service. Safe to deliver twice (deduped by event_id)."""
    if event.type not in ev.CONSUMED:
        return
    settings = settings or get_order_settings()
    gateway = gateway or MockPaymentGateway(settings.mock_payment_fail_amount)
    async with session_factory() as session, session.begin():
        if not await mark_processed(session, ProcessedEvent, event.event_id):
            return
        payload = event.payload
        if event.type == ev.QUALITY_GRADED:
            # TODO(M1): payload as in the quality contract (#12): subjectType ORDER or HARVEST,
            # subjectId, result PASS or FAIL, resolution, grade, weightKg. Without subjectType it
            # is an order (or {orderId}).
            subject_type = _pick(payload, "subjectType", "subject_type") or "ORDER"
            if subject_type == "HARVEST":
                await harvest.on_harvest_graded(session, payload, settings)
            elif subject_type == "ORDER":
                await _order_graded(session, payload)
            return  # LISTING and others concern catalog
        if event.type == ev.RESERVATION_ACCEPTED:
            await harvest.on_reservation_accepted(session, payload)
            return
        if event.type == ev.HARVEST_READY:
            await harvest.on_harvest_ready(session, payload)
            return
        if event.type == ev.RESERVATION_CANCELLED:
            await harvest.on_reservation_cancelled(session, payload)
            return
        if event.type == ev.POOL_ASSIGNED:
            # TODO(M1): assumed payload {orderIds: [...]} (or {orderId}); owner M2 (#19)
            steps: list[tuple[OrderStatus, str | None]] = [(S.POOLED, ev.ORDER_POOLED)]
            expected, note = {S.READY_FOR_DISPATCH}, "Delivery pooled"
        elif event.type == ev.DELIVERY_STARTED:
            steps, expected, note = (
                [(S.IN_TRANSIT, ev.ORDER_IN_TRANSIT)],
                {S.READY_FOR_DISPATCH, S.POOLED},
                "Picked up",
            )
        else:  # delivery.completed
            for order_id in _order_ids(payload):
                order = await _load(session, order_id)
                if order is not None and order.order_type == "HARVEST":
                    await harvest.complete_delivery(session, order_id, gateway)
                else:
                    await _advance(
                        session,
                        order_id,
                        {S.READY_FOR_DISPATCH, S.POOLED, S.IN_TRANSIT},
                        [(S.DELIVERED, None), (S.COMPLETED, ev.ORDER_COMPLETED)],
                        "Delivered",
                    )
            return
        for order_id in _order_ids(payload):
            await _advance(session, order_id, expected, steps, note)
