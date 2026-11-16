"""Example data returned by the contract stubs, until the real handlers land (issue #10)."""

from datetime import UTC, date, datetime
from uuid import UUID

from catalog_service.crops import CROPS
from catalog_service.geo import GeoPoint
from catalog_service.schemas import (
    CropOut,
    FarmOut,
    Grade,
    ListingFeature,
    ListingOut,
    ListingSearchProperties,
    ListingStatus,
    Unit,
)

FARMER_ID = UUID("6f1c2b7e-0000-4000-8000-000000000001")
FARM_ID = UUID("0b6c8d1e-0000-4000-8000-000000000010")
LISTING_ID = UUID("5a7e9f20-0000-4000-8000-000000000100")
NOW = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)

CROP_LIST = [CropOut(id=c, name=n, category=cat, default_unit=Unit(u)) for c, n, cat, u in CROPS]


def farm(**overrides: object) -> FarmOut:
    data: dict[str, object] = {
        "id": FARM_ID,
        "farmer_id": FARMER_ID,
        "name": "Sahu Organic Farm",
        "village": "Kumhari",
        "district": "Durg",
        "state": "Chhattisgarh",
        "location": GeoPoint.at(lat=21.2665, lng=81.5107),
        "boundary": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(overrides)
    return FarmOut.model_validate(data)


def listing(**overrides: object) -> ListingOut:
    data: dict[str, object] = {
        "id": LISTING_ID,
        "farm_id": FARM_ID,
        "farmer_id": FARMER_ID,
        "farm_name": "Sahu Organic Farm",
        "district": "Durg",
        "village": "Kumhari",
        "crop_id": "tomato",
        "crop_name": "Tomato",
        "variety": "Desi",
        "description": "Vine-ripened, picked this morning.",
        "qty_available": 120.0,
        "unit": Unit.KG,
        "price_per_unit": 28.0,
        "harvest_date": date(2026, 11, 14),
        "grade": Grade.A,
        "is_organic": True,
        "status": ListingStatus.ACTIVE,
        "pickup_location": GeoPoint.at(lat=21.2665, lng=81.5107),
        "media_ids": [],
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(overrides)
    return ListingOut.model_validate(data)


def search_feature(distance_m: float = 2350.0) -> ListingFeature:
    hit = listing()
    return ListingFeature(
        id=hit.id,
        geometry=hit.pickup_location,
        properties=ListingSearchProperties(
            **hit.model_dump(exclude={"pickup_location"}), distance_m=distance_m
        ),
    )
