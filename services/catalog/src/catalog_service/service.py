"""Catalog business logic. Functions take a session and never commit; the caller owns the
transaction, so a state change and its outbox event always commit together."""

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from geoalchemy2 import WKBElement
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import mapping
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from catalog_service.db import Farm, Listing, OutboxEvent, StockMovement, StockReservation
from catalog_service.geo import GeoPoint, GeoRef
from catalog_service.schemas import (
    FarmOut,
    ListingOut,
    ListingStatus,
    ReleasedItem,
    ReservedItem,
    StockItemIn,
    StockUnavailableItem,
)
from farm_common.auth import CurrentUser, Role
from farm_common.errors import AppError, Forbidden, NotFound
from farm_common.events import Event
from farm_common.outbox import add_event

PRODUCER = "catalog"


def now() -> datetime:
    return datetime.now(UTC)


def to_decimal(value: float) -> Decimal:
    # via str so 0.1 stays 0.1 (Decimal(0.1) would be 0.1000000000000000055...)
    return Decimal(str(value))


# ---- geometry ------------------------------------------------------------------------------


def point_to_db(point: GeoPoint) -> WKBElement:
    return from_shape(point.to_shape(), srid=4326)


def point_from_db(value: WKBElement) -> GeoPoint:
    return GeoPoint.from_shape(to_shape(value))


def area_from_db(value: WKBElement | None) -> dict[str, Any] | None:
    return None if value is None else dict(mapping(to_shape(value)))


# ---- read models ---------------------------------------------------------------------------


def farm_out(farm: Farm) -> FarmOut:
    return FarmOut(
        id=farm.id,
        farmer_id=farm.farmer_id,
        name=farm.name,
        village=farm.village,
        district=farm.district,
        state=farm.state,
        location=point_from_db(farm.location),
        boundary=area_from_db(farm.boundary),
        created_at=farm.created_at,
        updated_at=farm.updated_at,
    )


def listing_fields(listing: Listing) -> dict[str, Any]:
    return {
        "id": listing.id,
        "farm_id": listing.farm_id,
        "farmer_id": listing.farmer_id,
        "farm_name": listing.farm.name,
        "district": listing.farm.district,
        "village": listing.farm.village,
        "crop_id": listing.crop_id,
        "crop_name": listing.crop.name,
        "variety": listing.variety,
        "description": listing.description,
        "qty_available": listing.qty_available,
        "unit": listing.unit,
        "price_per_unit": listing.price_per_unit,
        "harvest_date": listing.harvest_date,
        "grade": listing.grade,
        "is_organic": listing.is_organic,
        "status": listing.status,
        "pickup_location": point_from_db(listing.pickup_location),
        "media_ids": list(listing.media_ids or []),
        "created_at": listing.created_at,
        "updated_at": listing.updated_at,
    }


def listing_out(listing: Listing) -> ListingOut:
    return ListingOut.model_validate(listing_fields(listing))


# ---- lookups and ownership -----------------------------------------------------------------


async def get_farm(session: AsyncSession, farm_id: UUID) -> Farm:
    farm = await session.get(Farm, farm_id)
    if farm is None:
        raise NotFound("Farm not found", code="FARM_NOT_FOUND")
    return farm


async def get_listing(session: AsyncSession, listing_id: UUID, *, lock: bool = False) -> Listing:
    query = select(Listing).where(Listing.id == listing_id)
    if lock:
        # lock only the listing row (the joined farm/crop rows stay unlocked)
        query = query.with_for_update(of=Listing)
    listing = (await session.execute(query)).scalar_one_or_none()
    if listing is None:
        raise NotFound("Listing not found", code="LISTING_NOT_FOUND")
    return listing


def ensure_owner(user: CurrentUser, farmer_id: UUID, what: str) -> None:
    if str(farmer_id) != user.id and not user.has_any(Role.ADMIN):
        raise Forbidden(f"This {what} belongs to another farmer", code=f"NOT_{what.upper()}_OWNER")


def status_for_stock(status: str, qty: Decimal) -> str:
    """ACTIVE <-> SOLD_OUT follows the stock; PAUSED and ARCHIVED are the farmer's choice."""
    if status == ListingStatus.ACTIVE and qty <= 0:
        return ListingStatus.SOLD_OUT
    if status == ListingStatus.SOLD_OUT and qty > 0:
        return ListingStatus.ACTIVE
    return status


# ---- events --------------------------------------------------------------------------------


def listing_payload(listing: Listing) -> dict[str, Any]:
    return {
        "listingId": str(listing.id),
        "farmId": str(listing.farm_id),
        "farmerId": str(listing.farmer_id),
        "cropId": listing.crop_id,
        "cropName": listing.crop.name,
        "variety": listing.variety,
        "qtyAvailable": float(listing.qty_available),
        "unit": listing.unit,
        "pricePerUnit": float(listing.price_per_unit),
        "grade": listing.grade,
        "isOrganic": listing.is_organic,
        "status": listing.status,
        "pickupLocation": point_from_db(listing.pickup_location).model_dump(),
        "mediaIds": [str(m) for m in listing.media_ids or []],
    }


def publish(session: AsyncSession, event_type: str, payload: dict[str, Any]) -> None:
    add_event(session, OutboxEvent, Event(type=event_type, producer=PRODUCER, payload=payload))


# ---- stock ---------------------------------------------------------------------------------


async def _reserved_items(session: AsyncSession, order_id: UUID) -> list[ReservedItem]:
    """What an order reserved, at the price it reserved it for."""
    rows = await session.execute(
        select(StockMovement, Listing)
        .join(Listing, StockMovement.listing_id == Listing.id)
        .where(StockMovement.order_id == order_id, StockMovement.reason == "RESERVE")
        .order_by(Listing.id)
    )
    return [
        ReservedItem(
            listing_id=listing.id,
            farm_id=listing.farm_id,
            farmer_id=listing.farmer_id,
            crop_id=listing.crop_id,
            crop_name=listing.crop.name,
            variety=listing.variety,
            unit=listing.unit,  # type: ignore[arg-type]
            qty=float(-movement.delta),
            unit_price=float(movement.unit_price or listing.price_per_unit),
            grade=listing.grade,  # type: ignore[arg-type]
            pickup_location=point_from_db(listing.pickup_location),
        )
        for movement, listing in rows
    ]


async def reserve_stock(
    session: AsyncSession, order_id: UUID, items: list[StockItemIn]
) -> list[ReservedItem]:
    """Reserve every item or none. Each decrement is one conditional UPDATE, so concurrent
    checkouts can never take stock below zero (and the check constraint backs this up)."""
    claimed = await session.execute(
        insert(StockReservation)
        .values(order_id=order_id, status="RESERVED", created_at=now())
        .on_conflict_do_nothing(index_elements=[StockReservation.order_id])
        .returning(StockReservation.order_id)
    )
    if claimed.first() is None:
        # Seen this order before (a retry). The insert above waited for any in-flight attempt.
        existing = await session.get(StockReservation, order_id)
        if existing is not None and existing.status == "RELEASED":
            raise AppError(
                "ORDER_RELEASED", "Stock for this order was already released", status_code=409
            )
        return await _reserved_items(session, order_id)

    wanted: dict[UUID, Decimal] = defaultdict(Decimal)
    for item in items:
        wanted[item.listing_id] += to_decimal(item.qty)

    short: list[StockUnavailableItem] = []
    changed_status: list[UUID] = []
    # Fixed order (by id) so two multi-item checkouts can't deadlock each other.
    for listing_id in sorted(wanted):
        qty = wanted[listing_id]
        new_qty = Listing.qty_available - qty
        row = (
            await session.execute(
                update(Listing)
                .where(
                    Listing.id == listing_id,
                    Listing.status == ListingStatus.ACTIVE,
                    Listing.qty_available >= qty,
                )
                .values(
                    qty_available=new_qty,
                    status=case((new_qty == 0, ListingStatus.SOLD_OUT.value), else_=Listing.status),
                    updated_at=now(),
                )
                .returning(Listing.price_per_unit, Listing.status)
            )
        ).first()
        if row is None:
            short.append(await _why_not_reserved(session, listing_id, qty))
            continue
        price, status = row
        if status != ListingStatus.ACTIVE:
            changed_status.append(listing_id)
        session.add(
            StockMovement(
                listing_id=listing_id,
                order_id=order_id,
                delta=-qty,
                reason="RESERVE",
                unit_price=price,
                created_at=now(),
            )
        )

    if short:
        await session.rollback()
        raise AppError(
            "STOCK_UNAVAILABLE",
            "Some items are no longer available in the requested quantity",
            status_code=409,
            details=[s.model_dump(mode="json", by_alias=True) for s in short],
        )
    await _publish_updated(session, changed_status)
    await session.flush()
    return await _reserved_items(session, order_id)


async def _why_not_reserved(
    session: AsyncSession, listing_id: UUID, qty: Decimal
) -> StockUnavailableItem:
    current = (
        await session.execute(
            select(Listing.status, Listing.qty_available).where(Listing.id == listing_id)
        )
    ).first()
    if current is None:
        await session.rollback()
        raise NotFound(f"Listing {listing_id} not found", code="LISTING_NOT_FOUND")
    status, available = current
    if status not in (ListingStatus.ACTIVE, ListingStatus.SOLD_OUT):
        await session.rollback()
        raise AppError(
            "LISTING_NOT_AVAILABLE",
            f"Listing {listing_id} is {status.lower()}",
            status_code=409,
            details=[{"listingId": str(listing_id), "status": status}],
        )
    return StockUnavailableItem(
        listing_id=listing_id, requested=float(qty), available=float(available)
    )


async def release_stock(session: AsyncSession, order_id: UUID) -> list[ReleasedItem]:
    """Give back whatever is still reserved for an order. Safe to call twice, or before reserve
    (a late reserve for a released order is then refused with ORDER_RELEASED)."""
    claimed = await session.execute(
        insert(StockReservation)
        .values(order_id=order_id, status="RELEASED", created_at=now(), released_at=now())
        .on_conflict_do_nothing(index_elements=[StockReservation.order_id])
        .returning(StockReservation.order_id)
    )
    if claimed.first() is not None:
        return []  # nothing was ever reserved; the tombstone blocks a late reserve
    reservation = (
        await session.execute(
            select(StockReservation).where(StockReservation.order_id == order_id).with_for_update()
        )
    ).scalar_one()
    if reservation.status == "RELEASED":
        return []

    net = await session.execute(
        select(StockMovement.listing_id, func.sum(StockMovement.delta))
        .where(StockMovement.order_id == order_id)
        .group_by(StockMovement.listing_id)
        .order_by(StockMovement.listing_id)
    )
    released: list[ReleasedItem] = []
    changed_status: list[UUID] = []
    for listing_id, total in net:
        qty = -total
        if qty <= 0:
            continue
        status = (
            await session.execute(
                update(Listing)
                .where(Listing.id == listing_id)
                .values(
                    qty_available=Listing.qty_available + qty,
                    status=case(
                        (
                            Listing.status == ListingStatus.SOLD_OUT,
                            ListingStatus.ACTIVE.value,
                        ),
                        else_=Listing.status,
                    ),
                    updated_at=now(),
                )
                .returning(Listing.status)
            )
        ).scalar_one()
        if status == ListingStatus.ACTIVE:
            changed_status.append(listing_id)
        session.add(
            StockMovement(
                listing_id=listing_id,
                order_id=order_id,
                delta=qty,
                reason="RELEASE",
                created_at=now(),
            )
        )
        released.append(ReleasedItem(listing_id=listing_id, qty=float(qty)))

    reservation.status = "RELEASED"
    reservation.released_at = now()
    await _publish_updated(session, changed_status)
    return released


async def _publish_updated(session: AsyncSession, listing_ids: list[UUID]) -> None:
    """listing.updated for listings whose status flipped (ACTIVE <-> SOLD_OUT)."""
    for listing_id in listing_ids:
        listing = await get_listing(session, listing_id)
        await session.refresh(listing)
        publish(session, "listing.updated", listing_payload(listing))


async def set_grade(session: AsyncSession, listing_ids: list[UUID], grade: str) -> int:
    changed = 0
    for listing_id in sorted(set(listing_ids)):
        listing = await session.get(Listing, listing_id)
        if listing is None or listing.grade == grade:
            continue
        listing.grade = grade
        listing.updated_at = now()
        publish(session, "listing.updated", listing_payload(listing))
        changed += 1
    return changed


# ---- geo reference (for media) -------------------------------------------------------------


async def geo_reference(session: AsyncSession, kind: str, ref_id: UUID) -> GeoRef:
    if kind == "farm":
        farm = await get_farm(session, ref_id)
        return GeoRef(point=point_from_db(farm.location), boundary=area_from_db(farm.boundary))
    listing = await get_listing(session, ref_id)
    return GeoRef(
        point=point_from_db(listing.pickup_location),
        boundary=area_from_db(listing.farm.boundary),
    )
