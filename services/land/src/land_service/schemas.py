from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from farm_common.schemas import ApiModel


class Point(ApiModel):
    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float]

    @model_validator(mode="after")
    def valid_coords(self):
        lng, lat = self.coordinates
        if not -180 <= lng <= 180 or not -90 <= lat <= 90:
            raise ValueError("coordinates must be [longitude, latitude] in WGS84")
        return self


class Polygon(ApiModel):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[tuple[float, float]]]

    @model_validator(mode="after")
    def valid_polygon(self):
        if len(self.coordinates) != 1 or len(self.coordinates[0]) < 4:
            raise ValueError("boundary must contain one exterior ring with at least four points")
        ring = self.coordinates[0]
        if ring[0] != ring[-1]:
            raise ValueError("polygon ring must be closed")
        for lng, lat in ring:
            if not -180 <= lng <= 180 or not -90 <= lat <= 90:
                raise ValueError("coordinates must be WGS84 [longitude, latitude]")
        return self


class PlotCreate(ApiModel):
    title: str = Field(min_length=2, max_length=120)
    description: str | None = None
    soil_type: str | None = Field(default=None, max_length=60)
    water_source: str | None = Field(default=None, max_length=60)
    boundary: Polygon


class PlotUpdate(ApiModel):
    title: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = None
    soil_type: str | None = Field(default=None, max_length=60)
    water_source: str | None = Field(default=None, max_length=60)
    boundary: Polygon | None = None


class PlotOut(ApiModel):
    id: UUID
    farmer_id: UUID
    title: str
    description: str | None
    area_acres: float
    soil_type: str | None
    water_source: str | None
    status: str
    boundary: dict[str, Any]
    centroid: dict[str, Any]
    created_at: datetime


class SeasonCreate(ApiModel):
    season: str = Field(min_length=2, max_length=40)
    start_date: date
    end_date: date
    price: float = Field(gt=0)
    allowed_crops: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid_dates(self):
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class SeasonOut(ApiModel):
    id: UUID
    plot_id: UUID
    season: str
    start_date: date
    end_date: date
    price: float
    allowed_crops: list[str]


class ReservationCreate(ApiModel):
    plot_season_id: UUID
    crop_id: str = Field(min_length=1, max_length=40)
    expected_qty_kg: float = Field(gt=0)


class ReservationOut(ApiModel):
    id: UUID
    plot_season_id: UUID
    customer_id: UUID
    farmer_id: UUID
    crop_id: str
    expected_qty_kg: float
    deposit: float | None
    total: float | None
    status: str
    expires_at: datetime


class AgreementOut(ApiModel):
    id: UUID
    reservation_id: UUID
    farmer_id: UUID
    customer_id: UUID
    plot_id: UUID
    plot_title: str
    season: str
    start_date: date
    end_date: date
    crop_id: str
    expected_qty_kg: float
    price_per_kg: float
    total: float
    deposit: float
    balance: float
    terms: list[str]
    created_at: datetime


class CropUpdateCreate(ApiModel):
    stage: Literal["SOWN", "GROWING", "FLOWERING"]
    note: str | None = None
    media_ids: list[UUID] = Field(default_factory=list, max_length=10)
    location: Point | None = None
