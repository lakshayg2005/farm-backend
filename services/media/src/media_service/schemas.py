"""Request and response bodies for the media API (camelCase JSON, see farm_common.schemas)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from farm_common.schemas import ApiModel

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ContentType = Literal["image/jpeg", "image/png", "image/webp"]
EXTENSIONS: dict[str, str] = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class OwnerType(StrEnum):
    """What the photo belongs to; decides which location it is checked against."""

    FARM = "FARM"  # catalog farm
    LISTING = "LISTING"  # catalog produce listing
    PLOT = "PLOT"  # land plot
    RESERVATION = "RESERVATION"  # land reservation (crop updates)
    DELIVERY = "DELIVERY"  # logistics proof of delivery (not geo-checked yet)
    USER = "USER"  # profile / team photo (never geo-checked)


# HLD section 10 stores owner_service: which service owns the thing a photo belongs to.
OWNER_SERVICES: dict[OwnerType, str] = {
    OwnerType.FARM: "catalog",
    OwnerType.LISTING: "catalog",
    OwnerType.PLOT: "land",
    OwnerType.RESERVATION: "land",
    OwnerType.DELIVERY: "logistics",
    OwnerType.USER: "identity",
}


class MediaStatus(StrEnum):
    PENDING_UPLOAD = "PENDING_UPLOAD"  # upload URL issued, waiting for /complete
    PROCESSING = "PROCESSING"  # uploaded; the worker is reading EXIF and making copies
    READY = "READY"
    REJECTED = "REJECTED"  # not a real image, or too big
    FAILED = "FAILED"  # processing kept failing
    DELETED = "DELETED"


class GeoStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"  # GPS within 2 km of the farm / plot
    TOO_FAR = "TOO_FAR"  # GPS more than 2 km away
    NO_GPS = "NO_GPS"  # no GPS in the photo (e.g. screenshot, or location turned off)
    NO_REFERENCE = "NO_REFERENCE"  # the farm / plot location could not be fetched
    NOT_APPLICABLE = "NOT_APPLICABLE"  # owner type is not location-bound


class UploadUrlIn(ApiModel):
    owner_type: OwnerType
    owner_id: UUID = Field(description="Id of the farm, listing, plot, reservation, ...")
    content_type: ContentType
    size_bytes: int = Field(gt=0, le=MAX_UPLOAD_BYTES, description="Exact file size (max 5 MB)")
    file_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = (
        None
    )


class UploadUrlOut(ApiModel):
    media_id: UUID
    upload_url: str = Field(description="PUT the file body here before `expiresAt`")
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str] = Field(description="Send exactly these headers with the PUT")
    expires_at: datetime


class MediaOut(ApiModel):
    id: UUID
    owner_service: str = Field(description="Service that owns the owner, e.g. catalog or land")
    owner_type: OwnerType
    owner_id: UUID
    uploaded_by: UUID
    status: MediaStatus
    content_type: str
    size_bytes: int
    url: str | None = Field(description="Public copy without EXIF/GPS; set once READY")
    thumb_url: str | None
    width: int | None
    height: int | None
    exif_time: datetime | None = Field(description="Capture time from EXIF, if present (UTC)")
    is_geo_verified: bool = Field(
        description="Whether the photo's GPS lies near the farm or plot. Informational; clients do not show it"
    )
    geo_status: GeoStatus = Field(description="Why it is (not) verified")
    distance_m: float | None = Field(description="Metres from the farm / plot (0 = inside it)")
    reject_reason: str | None
    created_at: datetime
    processed_at: datetime | None


class MediaPage(ApiModel):
    items: list[MediaOut]
    next_cursor: str | None


class GeoPoint(ApiModel):
    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float] = Field(description="[lng, lat]")


class MediaInternalOut(MediaOut):
    """For other services only: includes where the photo was taken and a short-lived URL to
    the original."""

    taken_at: GeoPoint | None = Field(description="EXIF GPS position (GeoJSON)")
    original_url: str | None
