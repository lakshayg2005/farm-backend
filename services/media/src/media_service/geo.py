"""Was the photo taken at the farm or plot? HLD section 13:

    ST_DWithin(media.taken_at, plot.boundary, 2000)    (as geography, so in metres)

The owning service says where its thing is through an internal route returning
`{"point": GeoJSON Point, "boundary": GeoJSON (Multi)Polygon | null}`:

    FARM, LISTING      -> catalog  GET /internal/geo/farm/{id}, /internal/geo/listing/{id}
    PLOT, RESERVATION  -> land     GET /internal/geo/plot/{id}, /internal/geo/reservation/{id}

The photo is compared with the boundary when there is one (0 m inside it), else with the point.
Other owner types (inspection, delivery, user) are not checked: NOT_APPLICABLE.
"""

import json
import logging
from functools import lru_cache
from typing import Any, Protocol
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from media_service.db import Media
from media_service.schemas import OWNER_SERVICES, GeoStatus, OwnerType
from media_service.settings import MediaSettings, get_media_settings

logger = logging.getLogger(__name__)

# owner type -> path segment of the owner's /internal/geo route
GEO_KINDS: dict[OwnerType, str] = {
    OwnerType.FARM: "farm",
    OwnerType.LISTING: "listing",
    OwnerType.PLOT: "plot",
    OwnerType.RESERVATION: "reservation",
}


def reference_geometry(reference: dict[str, Any]) -> dict[str, Any] | None:
    """The shape to measure against: the boundary if known, else the point."""
    shape = reference.get("boundary") or reference.get("point")
    if not isinstance(shape, dict) or shape.get("type") not in (
        "Point",
        "Polygon",
        "MultiPolygon",
    ):
        return None
    return shape


async def check_location(
    session: AsyncSession, media: Media, reference: dict[str, Any] | None, radius_m: float
) -> tuple[GeoStatus, float | None]:
    """Compare the photo's taken_at (already flushed) with the reference, in PostGIS."""
    if media.taken_at is None:
        return GeoStatus.NO_GPS, None
    shape = reference_geometry(reference) if reference else None
    if shape is None:
        return GeoStatus.NO_REFERENCE, None
    target = func.geography(func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(shape)), 4326))
    taken = func.geography(Media.taken_at)
    within, distance = (
        await session.execute(
            select(func.ST_DWithin(taken, target, radius_m), func.ST_Distance(taken, target)).where(
                Media.id == media.id
            )
        )
    ).one()
    return (GeoStatus.VERIFIED if within else GeoStatus.TOO_FAR), round(float(distance), 1)


class GeoResolver(Protocol):
    async def locate(self, owner_type: OwnerType, owner_id: UUID) -> dict[str, Any] | None: ...


class HttpGeoResolver:
    def __init__(self, settings: MediaSettings, timeout: float = 3.0) -> None:
        self.base_urls = {
            "catalog": settings.catalog_internal_url.rstrip("/"),
            "land": settings.land_internal_url.rstrip("/"),
        }
        self.timeout = timeout

    async def locate(self, owner_type: OwnerType, owner_id: UUID) -> dict[str, Any] | None:
        kind = GEO_KINDS.get(owner_type)
        if kind is None:
            return None
        url = f"{self.base_urls[OWNER_SERVICES[owner_type]]}/internal/geo/{kind}/{owner_id}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
            if response.status_code == 404:
                logger.warning("No %s %s behind %s", kind, owner_id, url)
                return None
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("Could not get the location of %s %s from %s", kind, owner_id, url)
            return None
        return body if isinstance(body, dict) else None


@lru_cache
def get_resolver() -> GeoResolver:
    return HttpGeoResolver(get_media_settings())
