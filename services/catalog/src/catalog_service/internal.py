"""Internal routes: reachable only inside the Docker network, never through the gateway."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from catalog_service import service
from catalog_service.database import get_session
from catalog_service.geo import GeoRef
from catalog_service.schemas import (
    StockReleaseIn,
    StockReleaseOut,
    StockReserveIn,
    StockReserveOut,
)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/stock/reserve")
async def reserve_stock(
    body: StockReserveIn, session: AsyncSession = Depends(get_session)
) -> StockReserveOut:
    """Reserve stock for an order, all items or none (called by order at checkout).

    Idempotent per `orderId`: repeating the call returns the same reservation. Errors:
    409 STOCK_UNAVAILABLE (`details` lists each short item), 404 LISTING_NOT_FOUND,
    409 LISTING_NOT_AVAILABLE (paused or archived), 409 ORDER_RELEASED (already released).
    """
    items = await service.reserve_stock(session, body.order_id, body.items)
    await session.commit()
    return StockReserveOut(order_id=body.order_id, items=items)


@router.post("/stock/release")
async def release_stock(
    body: StockReleaseIn, session: AsyncSession = Depends(get_session)
) -> StockReleaseOut:
    """Give back everything still reserved for an order (saga compensation). Idempotent.

    Catalog also does this by itself when it receives `order.cancelled`.
    """
    items = await service.release_stock(session, body.order_id)
    await session.commit()
    return StockReleaseOut(order_id=body.order_id, items=items)


@router.get("/geo/{kind}/{ref_id}")
async def geo_reference(
    kind: Literal["farm", "listing"], ref_id: UUID, session: AsyncSession = Depends(get_session)
) -> GeoRef:
    """Where a farm or listing is, so media can check a photo was taken there (within 2 km)."""
    return await service.geo_reference(session, kind, ref_id)
