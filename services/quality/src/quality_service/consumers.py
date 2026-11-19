"""Create quality inspections from packed orders and harvest-ready reservations."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farm_common.events import Event
from farm_common.outbox import mark_processed
from quality_service.db import Inspection, InspectorLocation, ProcessedEvent
from quality_service.geo import point_to_db
from quality_service.schemas import Point


def event_point(value):
    if not isinstance(value, dict):
        return None
    coords = value.get("coordinates")
    if isinstance(coords, (list, tuple)) and len(coords) == 2:
        try:
            return Point(coordinates=(float(coords[0]), float(coords[1])))
        except (TypeError, ValueError):
            return None
    if "lng" in value and "lat" in value:
        try:
            return Point(coordinates=(float(value["lng"]), float(value["lat"])))
        except (TypeError, ValueError):
            return None
    return None


NEAREST_CANDIDATES = 5


async def _claim_nearest_inspector(session: AsyncSession, location) -> InspectorLocation | None:
    """Lock the nearest available inspector that no other transaction is claiming.

    PostgreSQL cannot row-lock through a KNN (`<->`) GiST index scan ("attempted to lock invisible
    tuple"), so read the nearest few without a lock, then lock each by primary key, skipping
    those another inspection is claiming right now.
    """
    candidates = await session.scalars(
        select(InspectorLocation.inspector_id)
        .where(InspectorLocation.available.is_(True))
        .order_by(InspectorLocation.location.op("<->")(location))
        .limit(NEAREST_CANDIDATES)
    )
    for inspector_id in candidates.all():
        inspector = await session.scalar(
            select(InspectorLocation)
            .where(
                InspectorLocation.inspector_id == inspector_id,
                InspectorLocation.available.is_(True),
            )
            .with_for_update(skip_locked=True)
        )
        if inspector is not None:
            return inspector
    return None


async def create_inspection(
    session: AsyncSession, subject_type: str, subject_id: str, location_value=None, attempt: int = 1
):
    subject_uuid = UUID(str(subject_id))
    existing = await session.scalar(
        select(Inspection).where(
            Inspection.subject_type == subject_type,
            Inspection.subject_id == subject_uuid,
            Inspection.attempt == attempt,
        )
    )
    if existing is not None:
        return existing
    point = event_point(location_value)
    location = point_to_db(point) if point else None
    inspector = await _claim_nearest_inspector(session, location) if location is not None else None
    if inspector is not None:
        inspector.available = False
    row = Inspection(
        subject_type=subject_type,
        subject_id=subject_uuid,
        attempt=attempt,
        inspector_id=inspector.inspector_id if inspector is not None else None,
        location=location,
        status="PENDING",
    )
    session.add(row)
    await session.flush()
    return row


async def on_order_packed(session: AsyncSession, event: Event) -> None:
    order_id = event.payload.get("orderId") or event.payload.get("id")
    items = event.payload.get("items") or []
    pickup = items[0].get("pickupLocation") if items and isinstance(items[0], dict) else None
    if order_id:
        await create_inspection(
            session,
            "ORDER",
            str(order_id),
            event.payload.get("pickupLocation") or event.payload.get("location") or pickup,
        )


async def on_harvest_ready(session: AsyncSession, event: Event) -> None:
    reservation_id = event.payload.get("reservationId")
    if reservation_id:
        await create_inspection(
            session,
            "HARVEST",
            str(reservation_id),
            event.payload.get("plotCentroid") or event.payload.get("location"),
            attempt=int(event.payload.get("attempt", 1)),
        )


HANDLERS: dict[str, Callable[[AsyncSession, Event], Awaitable[None]]] = {
    "order.packed": on_order_packed,
    "harvest.ready": on_harvest_ready,
}


def make_handler(session_factory: async_sessionmaker[AsyncSession]):
    async def handle(event: Event) -> None:
        handler = HANDLERS.get(event.type)
        if handler is None:
            return
        async with session_factory() as session, session.begin():
            if await mark_processed(session, ProcessedEvent, event.event_id):
                await handler(session, event)

    return handle
