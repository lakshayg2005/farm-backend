"""Was the photo taken at the farm or plot? (HLD: ST_DWithin(media.taken_at, plot.boundary, 2000))

The owning service tells us where its thing is through an internal route returning
`{"point": GeoJSON Point, "boundary": GeoJSON (Multi)Polygon | null}`:

    FARM, LISTING      -> catalog  GET /internal/geo/farm/{id}, /internal/geo/listing/{id}
    PLOT, RESERVATION  -> land     GET /internal/geo/plot/{id}, /internal/geo/reservation/{id}

Other owner types (inspection, delivery, user) are not checked (NOT_APPLICABLE).
"""

import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol
from uuid import UUID

import httpx
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from media_service.schemas import GeoStatus, OwnerType
from media_service.settings import MediaSettings, get_media_settings

logger = logging.getLogger(__name__)

# owner type -> (service, path segment)
GEO_SOURCES: dict[OwnerType, tuple[str, str]] = {
    OwnerType.FARM: ("catalog", "farm"),
    OwnerType.LISTING: ("catalog", "listing"),
    OwnerType.PLOT: ("land", "plot"),
    OwnerType.RESERVATION: ("land", "reservation"),
}

EARTH_RADIUS_M = 6_371_008.8  # mean radius; a sphere is within ~0.5% of the ellipsoid
M_PER_DEG = EARTH_RADIUS_M * math.pi / 180


@dataclass
class GeoReference:
    point: Point
    boundary: BaseGeometry | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GeoReference":
        point = shape(data["point"])
        boundary = shape(data["boundary"]) if data.get("boundary") else None
        assert isinstance(point, Point)
        return cls(point=point, boundary=boundary)


def distance_m(lat: float, lng: float, ref: GeoReference) -> float:
    """Metres from the photo to the boundary (0 inside it), or to the point if no boundary.

    Uses a flat projection centred on the reference: accurate to well under 1% at a few km.
    """
    lat0, lng0 = ref.point.y, ref.point.x
    scale_x = M_PER_DEG * math.cos(math.radians(lat0))

    def to_metres(x: Any, y: Any, z: Any = None) -> tuple[Any, Any]:
        return ((x - lng0) * scale_x, (y - lat0) * M_PER_DEG)

    photo = transform(to_metres, Point(lng, lat))
    target = transform(to_metres, ref.boundary if ref.boundary is not None else ref.point)
    return float(photo.distance(target))


def classify(
    lat: float | None, lng: float | None, ref: GeoReference | None, radius_m: float
) -> tuple[GeoStatus, float | None]:
    if lat is None or lng is None:
        return GeoStatus.NO_GPS, None
    if ref is None:
        return GeoStatus.NO_REFERENCE, None
    distance = round(distance_m(lat, lng, ref), 1)
    return (GeoStatus.VERIFIED if distance <= radius_m else GeoStatus.TOO_FAR), distance


class GeoResolver(Protocol):
    async def locate(self, owner_type: OwnerType, owner_id: UUID) -> GeoReference | None: ...


class HttpGeoResolver:
    def __init__(self, settings: MediaSettings, timeout: float = 3.0) -> None:
        self.base_urls = {
            "catalog": settings.catalog_internal_url.rstrip("/"),
            "land": settings.land_internal_url.rstrip("/"),
        }
        self.timeout = timeout

    async def locate(self, owner_type: OwnerType, owner_id: UUID) -> GeoReference | None:
        source = GEO_SOURCES.get(owner_type)
        if source is None:
            return None
        service, kind = source
        url = f"{self.base_urls[service]}/internal/geo/{kind}/{owner_id}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
            if response.status_code == 404:
                logger.warning("No %s %s at %s", kind, owner_id, service)
                return None
            response.raise_for_status()
            return GeoReference.from_json(response.json())
        except (httpx.HTTPError, KeyError, ValueError, AssertionError):
            logger.warning("Could not get the location of %s %s from %s", kind, owner_id, url)
            return None


@lru_cache
def get_resolver() -> GeoResolver:
    return HttpGeoResolver(get_media_settings())
