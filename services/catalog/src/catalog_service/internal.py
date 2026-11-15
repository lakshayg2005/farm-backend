"""Internal routes: reachable only inside the Docker network, never through the gateway.

CONTRACT STUBS (issue #7); implemented in issue #10.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter

from catalog_service import examples
from catalog_service.geo import GeoPoint, GeoRef
from catalog_service.schemas import (
    ReleasedItem,
    ReservedItem,
    StockReleaseIn,
    StockReleaseOut,
    StockReserveIn,
    StockReserveOut,
)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/stock/reserve")
async def reserve_stock(body: StockReserveIn) -> StockReserveOut:
    """Reserve stock for an order, all items or none (called by order at checkout).

    Idempotent per `orderId`: repeating the call returns the same reservation. Errors:
    409 STOCK_UNAVAILABLE (`details` lists each short item), 404 LISTING_NOT_FOUND,
    409 LISTING_NOT_AVAILABLE (paused or archived).
    """
    sample = examples.listing()
    return StockReserveOut(
        order_id=body.order_id,
        items=[
            ReservedItem(
                listing_id=item.listing_id,
                farm_id=sample.farm_id,
                farmer_id=sample.farmer_id,
                crop_id=sample.crop_id,
                crop_name=sample.crop_name,
                variety=sample.variety,
                unit=sample.unit,
                qty=item.qty,
                unit_price=sample.price_per_unit,
                grade=sample.grade,
                pickup_location=sample.pickup_location,
            )
            for item in body.items
        ],
    )


@router.post("/stock/release")
async def release_stock(body: StockReleaseIn) -> StockReleaseOut:
    """Give back everything still reserved for an order (saga compensation). Idempotent.

    Catalog also does this by itself when it receives `order.cancelled`.
    """
    return StockReleaseOut(
        order_id=body.order_id, items=[ReleasedItem(listing_id=examples.LISTING_ID, qty=2.0)]
    )


@router.get("/geo/{kind}/{ref_id}")
async def geo_reference(kind: Literal["farm", "listing"], ref_id: UUID) -> GeoRef:
    """Where a farm or listing is, so media can check a photo was taken there (within 2 km)."""
    return GeoRef(point=GeoPoint.at(lat=21.2665, lng=81.5107), boundary=None)
