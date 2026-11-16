"""GeoJSON request/response types for the catalog API (coordinates are [lng, lat], SRID 4326)."""

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, model_validator
from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

from farm_common.schemas import ApiModel

Lng = Annotated[float, Field(ge=-180, le=180)]
Lat = Annotated[float, Field(ge=-90, le=90)]


class GeoPoint(ApiModel):
    """A GeoJSON Point: `{"type": "Point", "coordinates": [lng, lat]}`."""

    type: Literal["Point"] = "Point"
    coordinates: tuple[Lng, Lat]

    @classmethod
    def at(cls, lat: float, lng: float) -> "GeoPoint":
        return cls(coordinates=(lng, lat))

    @property
    def lng(self) -> float:
        return self.coordinates[0]

    @property
    def lat(self) -> float:
        return self.coordinates[1]

    def to_shape(self) -> Point:
        return Point(self.lng, self.lat)

    @classmethod
    def from_shape(cls, point: BaseGeometry) -> "GeoPoint":
        assert isinstance(point, Point)
        return cls(coordinates=(point.x, point.y))


def _check_area(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("boundary must be a GeoJSON Polygon or MultiPolygon")
    try:
        geom = shape(value)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValueError("boundary is not valid GeoJSON") from exc
    if geom.is_empty or not geom.is_valid:
        raise ValueError("boundary polygon is not valid (self-intersecting or unclosed)")
    min_x, min_y, max_x, max_y = geom.bounds
    if min_x < -180 or max_x > 180 or min_y < -90 or max_y > 90:
        raise ValueError("boundary coordinates must be [lng, lat] in degrees")
    return value


GeoArea = Annotated[
    dict[str, Any],
    AfterValidator(_check_area),
    Field(
        description="GeoJSON Polygon or MultiPolygon ([lng, lat] rings); stored as a MultiPolygon",
        examples=[
            {
                "type": "Polygon",
                "coordinates": [
                    [[81.28, 21.19], [81.29, 21.19], [81.29, 21.2], [81.28, 21.2], [81.28, 21.19]]
                ],
            }
        ],
    ),
]


def area_to_multipolygon(value: dict[str, Any]) -> MultiPolygon:
    geom = shape(value)
    return MultiPolygon([geom]) if isinstance(geom, Polygon) else geom  # type: ignore[return-value]


class GeoRef(ApiModel):
    """Where something is on the ground: a point and, if known, its boundary (internal use)."""

    point: GeoPoint
    boundary: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _boundary_is_area(self) -> "GeoRef":
        if self.boundary is not None:
            _check_area(self.boundary)
        return self
