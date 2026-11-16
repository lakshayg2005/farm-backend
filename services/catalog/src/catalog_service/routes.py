"""Public routes (behind the gateway at /api/v1/catalog).

CONTRACT STUBS (issue #7): every handler returns example data so the website and the apps can
build against the spec. The real implementation replaces the bodies in issue #10.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status

from catalog_service import examples
from catalog_service.schemas import (
    CropOut,
    FarmIn,
    FarmOut,
    FarmPage,
    FarmUpdate,
    Grade,
    ListingFeatureCollection,
    ListingIn,
    ListingOut,
    ListingPage,
    ListingStatus,
    ListingUpdate,
    SearchSort,
)
from farm_common.auth import CurrentUser, Role, require_roles

router = APIRouter()
farmer_only = require_roles(Role.FARMER)
farmer_or_admin = require_roles(Role.FARMER, Role.ADMIN)

LIMIT = Query(default=20, ge=1, le=100)
# HLD section 15: write calls accept an Idempotency-Key, so a retry never creates a duplicate.
IDEMPOTENCY_KEY = Header(
    default=None,
    alias="Idempotency-Key",
    min_length=8,
    max_length=128,
    description="Optional. Repeating a request with the same key returns the first response; "
    "the same key with a different body gives 409 IDEMPOTENCY_KEY_REUSED.",
)


@router.get("/crops", tags=["crops"])
async def list_crops() -> list[CropOut]:
    """The crop reference list for listing forms and search filters."""
    return examples.CROP_LIST


# ---- farms ---------------------------------------------------------------------------------


@router.post("/farms", status_code=status.HTTP_201_CREATED, tags=["farms"])
async def create_farm(
    body: FarmIn,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    user: CurrentUser = Depends(farmer_only),
) -> FarmOut:
    return examples.farm(farmer_id=UUID(user.id), **body.model_dump(exclude_none=True))


@router.get("/farms/mine", tags=["farms"])
async def my_farms(
    limit: int = LIMIT,
    cursor: str | None = None,
    user: CurrentUser = Depends(farmer_only),
) -> FarmPage:
    """The calling farmer's farms, newest first."""
    return FarmPage(items=[examples.farm(farmer_id=UUID(user.id))], next_cursor=None)


@router.get("/farms/{farm_id}", tags=["farms"])
async def get_farm(farm_id: UUID) -> FarmOut:
    return examples.farm(id=farm_id)


@router.patch("/farms/{farm_id}", tags=["farms"])
async def update_farm(
    farm_id: UUID, body: FarmUpdate, user: CurrentUser = Depends(farmer_or_admin)
) -> FarmOut:
    """Owner (or admin) only."""
    return examples.farm(id=farm_id, **body.model_dump(exclude_none=True))


@router.get("/farms/{farm_id}/listings", tags=["listings"])
async def farm_listings(
    farm_id: UUID, limit: int = LIMIT, cursor: str | None = None
) -> ListingPage:
    """A farm's ACTIVE listings (a farm's public page)."""
    return ListingPage(items=[examples.listing(farm_id=farm_id)], next_cursor=None)


# ---- listings ------------------------------------------------------------------------------


@router.post("/listings", status_code=status.HTTP_201_CREATED, tags=["listings"])
async def create_listing(
    body: ListingIn,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    user: CurrentUser = Depends(farmer_only),
) -> ListingOut:
    """Publish produce on one of your farms. Publishes `listing.published`.

    Errors: 404 FARM_NOT_FOUND, 403 NOT_FARM_OWNER, 422 UNKNOWN_CROP,
    409 IDEMPOTENCY_KEY_REUSED.
    """
    fields = body.model_dump(exclude_none=True)
    return examples.listing(farmer_id=UUID(user.id), grade=None, **fields)


@router.get("/listings/mine", tags=["listings"])
async def my_listings(
    status_filter: ListingStatus | None = Query(default=None, alias="status"),
    farm_id: UUID | None = None,
    limit: int = LIMIT,
    cursor: str | None = None,
    user: CurrentUser = Depends(farmer_only),
) -> ListingPage:
    """The calling farmer's listings in every status (ARCHIVED only if asked for), newest first."""
    return ListingPage(items=[examples.listing(farmer_id=UUID(user.id))], next_cursor=None)


@router.get("/listings/search", tags=["listings"])
async def search_listings(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius_km: float = Query(default=10, gt=0, le=100),
    crop: str | None = Query(default=None, description="Crop id, e.g. 'tomato'"),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    grade: Grade | None = Query(
        default=None, description="Minimum grade: B returns A and B (ungraded listings excluded)"
    ),
    organic: bool | None = None,
    sort: SearchSort = SearchSort.DISTANCE,
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = None,
) -> ListingFeatureCollection:
    """Produce near a point (V1), as a GeoJSON FeatureCollection (HLD sections 13 and 15).

    Each feature's geometry is the pickup point and its properties are the listing plus
    `distanceM` (metres). Only ACTIVE listings with stock.
    """
    return ListingFeatureCollection(features=[examples.search_feature()], next_cursor=None)


@router.get("/listings/{listing_id}", tags=["listings"])
async def get_listing(listing_id: UUID) -> ListingOut:
    return examples.listing(id=listing_id)


@router.patch("/listings/{listing_id}", tags=["listings"])
async def update_listing(
    listing_id: UUID, body: ListingUpdate, user: CurrentUser = Depends(farmer_or_admin)
) -> ListingOut:
    """Edit price or stock, pause or resume. Owner (or admin) only. Publishes `listing.updated`."""
    return examples.listing(id=listing_id, **body.model_dump(exclude_none=True))


@router.delete("/listings/{listing_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["listings"])
async def delete_listing(
    listing_id: UUID, user: CurrentUser = Depends(farmer_or_admin)
) -> Response:
    """Archive the listing (kept for order history). Owner (or admin) only."""
    return Response(status_code=status.HTTP_204_NO_CONTENT)
