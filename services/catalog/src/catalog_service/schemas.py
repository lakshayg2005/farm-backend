"""Request and response bodies for the catalog API (camelCase JSON, see farm_common.schemas)."""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from catalog_service.geo import GeoArea, GeoPoint
from farm_common.schemas import ApiModel

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
# Quantities are decimals (2.5 kg is fine); the database keeps 3 decimal places.
Quantity = Annotated[float, Field(gt=0, le=1_000_000)]
Price = Annotated[float, Field(gt=0, le=1_000_000, description="Rupees per unit")]


class Unit(StrEnum):
    KG = "kg"
    QUINTAL = "quintal"
    DOZEN = "dozen"
    PIECE = "piece"
    BUNCH = "bunch"
    LITRE = "litre"


class Grade(StrEnum):
    """Set by the quality service (quality.graded); A is best."""

    A = "A"
    B = "B"
    C = "C"


class ListingStatus(StrEnum):
    ACTIVE = "ACTIVE"  # visible in search
    PAUSED = "PAUSED"  # hidden by the farmer
    SOLD_OUT = "SOLD_OUT"  # stock reached 0 (back to ACTIVE when stock returns)
    ARCHIVED = "ARCHIVED"  # deleted by the farmer; kept for order history


class SearchSort(StrEnum):
    DISTANCE = "distance"
    PRICE = "price"
    NEWEST = "newest"


# ---- crops ---------------------------------------------------------------------------------


class CropOut(ApiModel):
    id: str = Field(description="Stable slug, e.g. 'tomato'; also used by the land service")
    name: str
    category: str
    default_unit: Unit


# ---- farms ---------------------------------------------------------------------------------


class FarmIn(ApiModel):
    name: Name
    village: ShortText | None = None
    district: ShortText
    state: ShortText | None = None
    location: GeoPoint = Field(description="Farm gate / main pickup point")
    boundary: GeoArea | None = None


class FarmUpdate(ApiModel):
    name: Name | None = None
    village: ShortText | None = None
    district: ShortText | None = None
    state: ShortText | None = None
    location: GeoPoint | None = None
    boundary: GeoArea | None = None


class FarmOut(ApiModel):
    id: UUID
    farmer_id: UUID
    name: str
    village: str | None
    district: str
    state: str | None
    location: GeoPoint
    boundary: dict[str, Any] | None = Field(description="GeoJSON MultiPolygon, if drawn")
    created_at: datetime
    updated_at: datetime


class FarmPage(ApiModel):
    items: list[FarmOut]
    next_cursor: str | None


# ---- listings ------------------------------------------------------------------------------


class ListingIn(ApiModel):
    farm_id: UUID
    crop_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    variety: ShortText | None = None
    description: LongText | None = None
    qty_available: Quantity
    unit: Unit = Unit.KG
    price_per_unit: Price
    harvest_date: date
    is_organic: bool = False
    pickup_location: GeoPoint | None = Field(
        default=None, description="Defaults to the farm's location"
    )
    media_ids: list[UUID] = Field(
        default_factory=list, max_length=10, description="Photos uploaded through media"
    )


class ListingUpdate(ApiModel):
    """Send only the fields to change. `qtyAvailable` sets the stock to this value."""

    variety: ShortText | None = None
    description: LongText | None = None
    qty_available: Annotated[float, Field(ge=0, le=1_000_000)] | None = None
    price_per_unit: Price | None = None
    harvest_date: date | None = None
    is_organic: bool | None = None
    pickup_location: GeoPoint | None = None
    media_ids: list[UUID] | None = Field(default=None, max_length=10)
    status: Literal[ListingStatus.ACTIVE, ListingStatus.PAUSED] | None = Field(
        default=None, description="Pause or resume the listing"
    )


class ListingOut(ApiModel):
    id: UUID
    farm_id: UUID
    farmer_id: UUID
    farm_name: str
    district: str
    village: str | None
    crop_id: str
    crop_name: str
    variety: str | None
    description: str | None
    qty_available: float
    unit: Unit
    price_per_unit: float
    harvest_date: date
    grade: Grade | None = Field(description="Null until the quality service grades a batch")
    is_organic: bool
    status: ListingStatus
    pickup_location: GeoPoint
    media_ids: list[UUID]
    created_at: datetime
    updated_at: datetime


class ListingPage(ApiModel):
    items: list[ListingOut]
    next_cursor: str | None


class ListingSearchHit(ListingOut):
    distance_m: float = Field(description="Distance from the search point, in metres")


class ListingSearchPage(ApiModel):
    items: list[ListingSearchHit]
    next_cursor: str | None


# ---- internal: stock -----------------------------------------------------------------------


class StockItemIn(ApiModel):
    listing_id: UUID
    qty: Quantity


class StockReserveIn(ApiModel):
    order_id: UUID
    items: list[StockItemIn] = Field(min_length=1, max_length=50)


class ReservedItem(ApiModel):
    """Everything order needs to write an order_item without another call."""

    listing_id: UUID
    farm_id: UUID
    farmer_id: UUID
    crop_id: str
    crop_name: str
    variety: str | None
    unit: Unit
    qty: float
    unit_price: float
    grade: Grade | None
    pickup_location: GeoPoint


class StockReserveOut(ApiModel):
    order_id: UUID
    items: list[ReservedItem]


class StockReleaseIn(ApiModel):
    order_id: UUID


class ReleasedItem(ApiModel):
    listing_id: UUID
    qty: float


class StockReleaseOut(ApiModel):
    order_id: UUID
    items: list[ReleasedItem] = Field(
        description="Empty if nothing was reserved or already released"
    )


class StockUnavailableItem(ApiModel):
    """One entry of `error.details` for a 409 STOCK_UNAVAILABLE."""

    listing_id: UUID
    requested: float
    available: float
