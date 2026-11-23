"""Request / response bodies for the logistics API (camelCase JSON via ApiModel)."""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AliasChoices, Field

from farm_common.schemas import ApiModel

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StopType(StrEnum):
    PICKUP = "PICKUP"
    DROP = "DROP"


class StopStatus(StrEnum):
    PENDING = "PENDING"
    ARRIVED = "ARRIVED"
    DONE = "DONE"
    SKIPPED = "SKIPPED"


class PoolStatus(StrEnum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


# ---------------------------------------------------------------------------
# Shared sub-shapes
# ---------------------------------------------------------------------------


class GeoPoint(ApiModel):
    lat: float
    lng: float


class StopOut(ApiModel):
    id: UUID
    pool_id: UUID
    order_id: UUID
    stop_type: StopType
    seq: int
    lat: float
    lng: float
    address: str | None = None
    eta: datetime | None = None
    status: StopStatus
    photo_media_id: UUID | None = None
    otp_verified: bool = False
    done_at: datetime | None = None
    weight_kg: float | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    notes: str | None = None
    otp: str | None = Field(
        default=None,
        description=(
            "The 4-digit delivery code of a DROP stop. Only the order's customer sees it (in "
            "GET /orders/{id}/track) and tells it to the partner at the door; it is never sent "
            "to the partner or in pool lists."
        ),
    )


class PoolOut(ApiModel):
    id: UUID
    pool_date: date
    partner_id: UUID | None
    status: PoolStatus
    total_km: float | None
    km_saved: float | None
    stops: list[StopOut] = Field(default_factory=list)
    route_path: list[tuple[float, float]] | None = Field(
        default=None,
        description=(
            "The road line through the stops in visiting order, as [lat, lng] pairs. Only sent "
            "by GET /my-route and when accepting an offer, and only when the routing engine "
            "found it; otherwise the client joins the stops with straight lines."
        ),
    )
    created_at: datetime
    assigned_at: datetime | None


# ---------------------------------------------------------------------------
# POST /logistics/pools/run
# ---------------------------------------------------------------------------


class PoolRunResponse(ApiModel):
    pools_created: int
    pools: list[PoolOut]


class BatchOrderOut(ApiModel):
    order_id: UUID
    address: str | None = None
    weight_kg: float
    delivery_date: date | None = None
    route: int = Field(description="Which of the estimated routes it would ride (1-based)")


class BatchPreview(ApiModel):
    """The shared-delivery batch as it would run now (nothing is created)."""

    batch_time: str = Field(description="Daily run time, India, HH:MM")
    next_run_at: datetime
    waiting_orders: int = Field(description="FLEXIBLE orders packed and waiting")
    due_orders: int = Field(description="Of those, how many the next run would take (due by then)")
    estimated_routes: int
    total_weight_kg: float
    orders: list[BatchOrderOut]


# ---------------------------------------------------------------------------
# Stop action requests
# ---------------------------------------------------------------------------


class StopActionIn(ApiModel):
    notes: str | None = None
    reason: str | None = None


class PickupIn(ApiModel):
    notes: str | None = None
    weight_kg: float | None = None
    collected_at: datetime | None = None


class DeliverIn(ApiModel):
    photo_media_id: UUID = Field(description="media_id of the proof-of-delivery photo")
    otp: str = Field(min_length=4, max_length=6, description="4-digit OTP entered by customer")
    notes: str | None = None
    delivered_at: datetime | None = None


# ---------------------------------------------------------------------------
# POST /logistics/location
# ---------------------------------------------------------------------------


class LocationPingIn(ApiModel):
    lat: float
    lng: float
    pool_id: UUID | None = None
    accuracy_m: float | None = Field(
        default=None,
        validation_alias=AliasChoices("accuracyM", "accuracy_m", "accuracy"),
    )
    speed_ms: float | None = Field(
        default=None,
        validation_alias=AliasChoices("speedMs", "speed_ms", "speed"),
    )
    heading: float | None = None
    recorded_at: datetime | None = None


# ---------------------------------------------------------------------------
# GET /logistics/orders/{id}/track
# ---------------------------------------------------------------------------


class TrackOut(ApiModel):
    order_id: UUID
    pool_id: UUID | None = None
    status: str | None = None
    stop: StopOut | None = None
    partner_id: UUID | None = None
    partner_name: str | None = None
    partner_phone: str | None = None
    partner_lat: float | None = Field(None, description="Latest GPS lat from location pings")
    partner_lng: float | None = Field(None, description="Latest GPS lng from location pings")
    partner_updated_at: datetime | None = None
    eta: datetime | None = None


class OfferPlace(ApiModel):
    lat: float
    lng: float
    address: str | None = None


class OfferStop(ApiModel):
    """One stop of an offered route, in driving order. No address or customer: the partner has
    not taken the route yet, so they only see where it goes."""

    seq: int
    stop_type: StopType
    lat: float
    lng: float


class OfferOut(ApiModel):
    """A pool offered to the signed-in partner, still open (nobody has accepted it)."""

    pool_id: UUID
    pool_date: date
    stop_count: int
    pickup_count: int
    drop_count: int
    total_km: float | None = None
    distance_km: float | None = Field(None, description="From the partner to the first pickup")
    first_pickup: OfferPlace | None = None
    stops: list[OfferStop] = Field(
        default_factory=list, description="Every stop in driving order, to draw the route"
    )
    offered_at: datetime
    expires_at: datetime | None = Field(
        None, description="Answer by this time; it stays open until someone accepts"
    )
