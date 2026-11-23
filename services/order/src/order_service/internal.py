"""Internal routes: reachable only inside the Docker network, never through the gateway."""

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from order_service.db import OrderRow
from order_service.deps import get_session
from order_service.schemas import DeliveryMode, OrderStatus

router = APIRouter(prefix="/internal", tags=["internal"])


def _address_line(address: dict[str, Any]) -> str:
    parts = [
        address.get("line1"),
        address.get("line2"),
        address.get("city"),
        address.get("pincode"),
    ]
    return ", ".join(str(p) for p in parts if p)


# Rough kilograms per unit, so a vehicle's capacity (in kg) means something for every crop.
KG_PER_UNIT: dict[str, Decimal] = {
    "kg": Decimal(1),
    "quintal": Decimal(100),
    "litre": Decimal(1),
    "dozen": Decimal("1.5"),
    "piece": Decimal("0.5"),
    "bunch": Decimal("0.25"),
}


def _weight_kg(quantity: Decimal, unit: str | None) -> Decimal:
    return quantity * KG_PER_UNIT.get((unit or "kg").lower(), Decimal(1))


@router.get(
    "/orders/ready-for-dispatch",
    summary="Orders logistics can pool (called by the logistics pooling job)",
)
async def ready_for_dispatch(
    pool_date: date = Query(alias="date", description="Pool orders due on or before this day"),
    mode: DeliveryMode | None = Query(
        default=None,
        description="Only IMMEDIATE or only FLEXIBLE orders; both when omitted",
    ),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Orders in READY_FOR_DISPATCH whose delivery date is `date` or earlier.

    Logistics pools IMMEDIATE orders right after packing and FLEXIBLE ones in the daily batch, so
    it asks for one mode at a time (`mode`); the admin "run now" button asks for both.

    The shape is what the logistics pooling job reads (snake_case keys, not the public camelCase):
    `order_id`, `delivery_mode`, `customer_id`, `hub_lat`/`hub_lng` (where the farm hands the produce over),
    `drop_lat`/`drop_lng` (the customer's address), `weight_kg`, `address` and `contact_phone`
    (so the partner can call the customer). Once logistics publishes `pool.assigned` the order
    moves to POOLED and no longer shows up here.
    """
    conditions = [
        OrderRow.status == OrderStatus.READY_FOR_DISPATCH.value,
        OrderRow.delivery_date <= pool_date,
        OrderRow.delivery_address.is_not(None),
    ]
    if mode is not None:
        conditions.append(OrderRow.delivery_mode == mode.value)
    rows = (
        (
            await session.execute(
                select(OrderRow)
                .where(*conditions)
                .options(selectinload(OrderRow.items))
                .order_by(OrderRow.created_at, OrderRow.id)
            )
        )
        .scalars()
        .all()
    )
    result = []
    for row in rows:
        if not row.items or row.delivery_address is None:
            continue
        hub_lng, hub_lat = row.items[0].pickup_location["coordinates"]
        drop_lng, drop_lat = row.delivery_address["location"]["coordinates"]
        result.append(
            {
                "order_id": str(row.id),
                "delivery_mode": row.delivery_mode,
                "delivery_date": row.delivery_date.isoformat() if row.delivery_date else None,
                "hub_lat": hub_lat,
                "hub_lng": hub_lng,
                "drop_lat": drop_lat,
                "drop_lng": drop_lng,
                "weight_kg": float(sum(_weight_kg(i.quantity, i.unit) for i in row.items)),
                "address": _address_line(row.delivery_address),
                "customer_id": str(row.customer_id),
                "contact_phone": row.delivery_address.get("contactPhone"),
            }
        )
    return result
