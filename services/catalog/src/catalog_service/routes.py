"""Public routes (behind the gateway at /api/v1/catalog)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from geoalchemy2.shape import from_shape
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalog_service import service
from catalog_service.database import get_session
from catalog_service.db import Crop, Farm, Listing, StockMovement
from catalog_service.geo import area_to_multipolygon
from catalog_service.schemas import (
    CropOut,
    FarmIn,
    FarmOut,
    FarmPage,
    FarmUpdate,
    Grade,
    ListingIn,
    ListingOut,
    ListingPage,
    ListingSearchHit,
    ListingSearchPage,
    ListingStatus,
    ListingUpdate,
    SearchSort,
)
from farm_common.auth import CurrentUser, Role, require_roles
from farm_common.errors import AppError

router = APIRouter()
farmer_only = require_roles(Role.FARMER)
farmer_or_admin = require_roles(Role.FARMER, Role.ADMIN)

LIMIT = Query(default=20, ge=1, le=100)
GRADES_AT_LEAST = {Grade.A: ["A"], Grade.B: ["A", "B"], Grade.C: ["A", "B", "C"]}


def _offset(cursor: str | None) -> int:
    return int(cursor) if cursor and cursor.isdigit() else 0


def _next_cursor(offset: int, limit: int, fetched: int) -> str | None:
    return str(offset + limit) if fetched > limit else None


async def _listing_page(
    session: AsyncSession, query: Select[Listing], limit: int, cursor: str | None
) -> ListingPage:
    offset = _offset(cursor)
    query = query.order_by(Listing.created_at.desc(), Listing.id).offset(offset).limit(limit + 1)
    rows = (await session.execute(query)).scalars().all()
    return ListingPage(
        items=[service.listing_out(r) for r in rows[:limit]],
        next_cursor=_next_cursor(offset, limit, len(rows)),
    )


@router.get("/crops", tags=["crops"])
async def list_crops(session: AsyncSession = Depends(get_session)) -> list[CropOut]:
    """The crop reference list for listing forms and search filters."""
    crops = (await session.execute(select(Crop).order_by(Crop.category, Crop.name))).scalars()
    return [CropOut.model_validate(c) for c in crops]


# ---- farms ---------------------------------------------------------------------------------


@router.post("/farms", status_code=status.HTTP_201_CREATED, tags=["farms"])
async def create_farm(
    body: FarmIn,
    user: CurrentUser = Depends(farmer_only),
    session: AsyncSession = Depends(get_session),
) -> FarmOut:
    farm = Farm(
        farmer_id=UUID(user.id),
        name=body.name,
        village=body.village,
        district=body.district,
        state=body.state,
        location=service.point_to_db(body.location),
        boundary=(
            from_shape(area_to_multipolygon(body.boundary), srid=4326) if body.boundary else None
        ),
    )
    session.add(farm)
    await session.commit()
    await session.refresh(farm)
    return service.farm_out(farm)


@router.get("/farms/mine", tags=["farms"])
async def my_farms(
    limit: int = LIMIT,
    cursor: str | None = None,
    user: CurrentUser = Depends(farmer_only),
    session: AsyncSession = Depends(get_session),
) -> FarmPage:
    """The calling farmer's farms, newest first."""
    offset = _offset(cursor)
    query = (
        select(Farm)
        .where(Farm.farmer_id == UUID(user.id))
        .order_by(Farm.created_at.desc(), Farm.id)
        .offset(offset)
        .limit(limit + 1)
    )
    farms = (await session.execute(query)).scalars().all()
    return FarmPage(
        items=[service.farm_out(f) for f in farms[:limit]],
        next_cursor=_next_cursor(offset, limit, len(farms)),
    )


@router.get("/farms/{farm_id}", tags=["farms"])
async def get_farm(farm_id: UUID, session: AsyncSession = Depends(get_session)) -> FarmOut:
    return service.farm_out(await service.get_farm(session, farm_id))


@router.patch("/farms/{farm_id}", tags=["farms"])
async def update_farm(
    farm_id: UUID,
    body: FarmUpdate,
    user: CurrentUser = Depends(farmer_or_admin),
    session: AsyncSession = Depends(get_session),
) -> FarmOut:
    """Owner (or admin) only. Changing the location does not move existing listings."""
    farm = await service.get_farm(session, farm_id)
    service.ensure_owner(user, farm.farmer_id, "farm")
    changes = body.model_dump(exclude_unset=True)
    for field in ("name", "district", "location"):
        if field in changes and changes[field] is None:
            raise AppError("VALIDATION_ERROR", f"{field} cannot be null", 422)
    for field in ("name", "village", "district", "state"):
        if field in changes:
            setattr(farm, field, changes[field])
    if body.location is not None:
        farm.location = service.point_to_db(body.location)
    if "boundary" in changes:
        farm.boundary = (
            from_shape(area_to_multipolygon(body.boundary), srid=4326) if body.boundary else None
        )
    await session.commit()
    await session.refresh(farm)
    return service.farm_out(farm)


@router.get("/farms/{farm_id}/listings", tags=["listings"])
async def farm_listings(
    farm_id: UUID,
    limit: int = LIMIT,
    cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> ListingPage:
    """A farm's ACTIVE listings (a farm's public page)."""
    await service.get_farm(session, farm_id)
    query = select(Listing).where(
        Listing.farm_id == farm_id, Listing.status == ListingStatus.ACTIVE
    )
    return await _listing_page(session, query, limit, cursor)


# ---- listings ------------------------------------------------------------------------------


@router.post("/listings", status_code=status.HTTP_201_CREATED, tags=["listings"])
async def create_listing(
    body: ListingIn,
    user: CurrentUser = Depends(farmer_only),
    session: AsyncSession = Depends(get_session),
) -> ListingOut:
    """Publish produce on one of your farms. Publishes `listing.published`.

    Errors: 404 FARM_NOT_FOUND, 403 NOT_FARM_OWNER, 422 UNKNOWN_CROP.
    """
    farm = await service.get_farm(session, body.farm_id)
    service.ensure_owner(user, farm.farmer_id, "farm")
    if await session.get(Crop, body.crop_id) is None:
        raise AppError("UNKNOWN_CROP", f"Unknown crop '{body.crop_id}'; see GET /crops", 422)

    qty = service.to_decimal(body.qty_available)
    listing = Listing(
        farm_id=farm.id,
        farmer_id=farm.farmer_id,
        crop_id=body.crop_id,
        variety=body.variety,
        description=body.description,
        qty_available=qty,
        unit=body.unit,
        price_per_unit=service.to_decimal(body.price_per_unit),
        harvest_date=body.harvest_date,
        is_organic=body.is_organic,
        status=ListingStatus.ACTIVE,
        pickup_location=(
            service.point_to_db(body.pickup_location) if body.pickup_location else farm.location
        ),
        media_ids=body.media_ids,
    )
    session.add(listing)
    await session.flush()
    session.add(StockMovement(listing_id=listing.id, delta=qty, reason="INITIAL"))
    await session.refresh(listing)
    service.publish(session, "listing.published", service.listing_payload(listing))
    await session.commit()
    return service.listing_out(listing)


@router.get("/listings/mine", tags=["listings"])
async def my_listings(
    status_filter: ListingStatus | None = Query(default=None, alias="status"),
    farm_id: UUID | None = Query(default=None, alias="farmId"),
    limit: int = LIMIT,
    cursor: str | None = None,
    user: CurrentUser = Depends(farmer_only),
    session: AsyncSession = Depends(get_session),
) -> ListingPage:
    """The calling farmer's listings in every status (ARCHIVED only if asked for), newest first."""
    query = select(Listing).where(Listing.farmer_id == UUID(user.id))
    if status_filter is not None:
        query = query.where(Listing.status == status_filter)
    else:
        query = query.where(Listing.status != ListingStatus.ARCHIVED)
    if farm_id is not None:
        query = query.where(Listing.farm_id == farm_id)
    return await _listing_page(session, query, limit, cursor)


@router.get("/listings/search", tags=["listings"])
async def search_listings(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius_km: float = Query(default=10, gt=0, le=100, alias="radiusKm"),
    crop: str | None = Query(default=None, description="Crop id, e.g. 'tomato'"),
    min_price: float | None = Query(default=None, ge=0, alias="minPrice"),
    max_price: float | None = Query(default=None, ge=0, alias="maxPrice"),
    grade: Grade | None = Query(
        default=None, description="Minimum grade: B returns A and B (ungraded listings excluded)"
    ),
    organic: bool | None = None,
    sort: SearchSort = SearchSort.DISTANCE,
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> ListingSearchPage:
    """Produce near a point (V1). Only ACTIVE listings with stock; `distanceM` is in metres."""
    here = func.geography(func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326))
    # Same expression as the listings_pickup_gix index, so the index is used.
    where = func.geography(Listing.pickup_location)
    distance = func.ST_Distance(where, here).label("distance_m")
    query = select(Listing, distance).where(
        Listing.status == ListingStatus.ACTIVE,
        Listing.qty_available > 0,
        func.ST_DWithin(where, here, radius_km * 1000),
    )
    if crop:
        query = query.where(Listing.crop_id == crop)
    if min_price is not None:
        query = query.where(Listing.price_per_unit >= min_price)
    if max_price is not None:
        query = query.where(Listing.price_per_unit <= max_price)
    if grade is not None:
        query = query.where(Listing.grade.in_(GRADES_AT_LEAST[grade]))
    if organic is not None:
        query = query.where(Listing.is_organic.is_(organic))
    order = {
        SearchSort.DISTANCE: (distance, Listing.id),
        SearchSort.PRICE: (Listing.price_per_unit, distance, Listing.id),
        SearchSort.NEWEST: (Listing.created_at.desc(), Listing.id),
    }[sort]

    offset = _offset(cursor)
    rows = (await session.execute(query.order_by(*order).offset(offset).limit(limit + 1))).all()
    return ListingSearchPage(
        items=[
            ListingSearchHit.model_validate(
                {**service.listing_fields(listing), "distance_m": round(dist, 1)}
            )
            for listing, dist in rows[:limit]
        ],
        next_cursor=_next_cursor(offset, limit, len(rows)),
    )


@router.get("/listings/{listing_id}", tags=["listings"])
async def get_listing(listing_id: UUID, session: AsyncSession = Depends(get_session)) -> ListingOut:
    listing = await service.get_listing(session, listing_id)
    if listing.status == ListingStatus.ARCHIVED:
        raise AppError("LISTING_NOT_FOUND", "Listing not found", 404)
    return service.listing_out(listing)


@router.patch("/listings/{listing_id}", tags=["listings"])
async def update_listing(
    listing_id: UUID,
    body: ListingUpdate,
    user: CurrentUser = Depends(farmer_or_admin),
    session: AsyncSession = Depends(get_session),
) -> ListingOut:
    """Edit price or stock, pause or resume. Owner (or admin) only. Publishes `listing.updated`.

    `qtyAvailable` sets the stock; the row is locked so it can't race a checkout.
    """
    listing = await service.get_listing(session, listing_id, lock=True)
    service.ensure_owner(user, listing.farmer_id, "listing")
    if listing.status == ListingStatus.ARCHIVED:
        raise AppError("LISTING_ARCHIVED", "An archived listing can't be edited", 409)

    changes = body.model_dump(exclude_unset=True)
    for field in ("price_per_unit", "harvest_date", "is_organic", "qty_available", "status"):
        if field in changes and changes[field] is None:
            raise AppError("VALIDATION_ERROR", f"{field} cannot be null", 422)
    for field in ("variety", "description", "harvest_date", "is_organic"):
        if field in changes:
            setattr(listing, field, changes[field])
    if body.price_per_unit is not None:
        listing.price_per_unit = service.to_decimal(body.price_per_unit)
    if body.pickup_location is not None:
        listing.pickup_location = service.point_to_db(body.pickup_location)
    if body.media_ids is not None:
        listing.media_ids = body.media_ids
    if body.qty_available is not None:
        new_qty = service.to_decimal(body.qty_available)
        delta = new_qty - listing.qty_available
        if delta:
            session.add(StockMovement(listing_id=listing.id, delta=delta, reason="ADJUSTMENT"))
        listing.qty_available = new_qty
    if body.status is not None:
        listing.status = body.status
    listing.status = service.status_for_stock(listing.status, listing.qty_available)

    await session.flush()
    await session.refresh(listing)
    service.publish(session, "listing.updated", service.listing_payload(listing))
    await session.commit()
    return service.listing_out(listing)


@router.delete("/listings/{listing_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["listings"])
async def delete_listing(
    listing_id: UUID,
    user: CurrentUser = Depends(farmer_or_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Archive the listing (kept for order history). Owner (or admin) only."""
    listing = await service.get_listing(session, listing_id, lock=True)
    service.ensure_owner(user, listing.farmer_id, "listing")
    if listing.status != ListingStatus.ARCHIVED:
        listing.status = ListingStatus.ARCHIVED
        await session.flush()
        await session.refresh(listing)
        service.publish(session, "listing.updated", service.listing_payload(listing))
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
