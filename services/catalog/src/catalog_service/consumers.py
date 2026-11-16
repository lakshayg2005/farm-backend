"""Events catalog reacts to. Each handler is idempotent (processed_events dedupe by event_id).

Payload fields read (everything else is ignored, so producers can add fields freely):

- order.cancelled:  {"orderId": "<uuid>"}  -> release whatever catalog reserved for that order
- quality.graded:   {"grade": "A"|"B"|"C", "subjectType": "LISTING", "subjectId": "<uuid>"}
                    and/or {"grade": ..., "listingIds": ["<uuid>", ...]}  -> set the listing grade
"""

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from catalog_service import service
from catalog_service.db import ProcessedEvent
from farm_common.events import Event
from farm_common.outbox import mark_processed

logger = logging.getLogger(__name__)

BINDINGS = ["order.cancelled", "quality.graded"]


def _uuids(values: object) -> list[UUID]:
    if not isinstance(values, list):
        return []
    out = []
    for value in values:
        try:
            out.append(UUID(str(value)))
        except ValueError:
            logger.warning("Ignoring invalid listing id %r", value)
    return out


async def on_order_cancelled(session: AsyncSession, event: Event) -> None:
    order_id = event.payload.get("orderId")
    if not order_id:
        logger.warning("order.cancelled %s has no orderId", event.event_id)
        return
    released = await service.release_stock(session, UUID(str(order_id)))
    logger.info("Released %d listing(s) for cancelled order %s", len(released), order_id)


async def on_quality_graded(session: AsyncSession, event: Event) -> None:
    grade = event.payload.get("grade")
    if grade not in ("A", "B", "C"):
        logger.info(
            "quality.graded %s has grade %r; listing grade unchanged", event.event_id, grade
        )
        return
    listing_ids = _uuids(event.payload.get("listingIds"))
    if event.payload.get("subjectType") == "LISTING":
        listing_ids += _uuids([event.payload.get("subjectId")])
    if listing_ids:
        await service.set_grade(session, listing_ids, grade)


HANDLERS: dict[str, Callable[[AsyncSession, Event], Awaitable[None]]] = {
    "order.cancelled": on_order_cancelled,
    "quality.graded": on_quality_graded,
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
                return  # duplicate delivery
            await handler(session, event)

    return handle
