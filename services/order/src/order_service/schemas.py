"""API contract of the order service: request and response models.

These models are the contract the customer app (and the website) build against. Changing a field
name or type here is a contract change and needs approval from the other team.

Conventions (match the rest of the backend):
- JSON is camelCase (farm_common.schemas.ApiModel), like every other service; ids are UUIDs;
  timestamps are timezone-aware ISO 8601.
- Money is a number in rupees with two decimals; `currency` is always "INR".
- `qty` is in the listing's own unit (usually kg), named like the catalog's stock API.
- Places are GeoJSON points `{"type": "Point", "coordinates": [lng, lat]}`, like the catalog (HLD section 15).
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from farm_common.schemas import ApiModel


class OrderStatus(StrEnum):
    """Lifecycle of a per-farm order (HLD section 11).

    PLACED -> CONFIRMED -> PACKED -> QC_PASSED -> READY_FOR_DISPATCH is issue #13. After that
    logistics drives it: POOLED (pool assigned), IN_TRANSIT (picked up), DELIVERED, COMPLETED.
    """

    PLACED = "PLACED"
    CONFIRMED = "CONFIRMED"
    PACKED = "PACKED"
    QC_PASSED = "QC_PASSED"
    READY_FOR_DISPATCH = "READY_FOR_DISPATCH"
    POOLED = "POOLED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class OrderType(StrEnum):
    """PRODUCE is the V1 purchase of listed produce. HARVEST is added by the V2 harvest flow."""

    PRODUCE = "PRODUCE"
    HARVEST = "HARVEST"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class PaymentKind(StrEnum):
    """What a payment is for (HLD section 10). The land service pays reservation deposits through
    this API too. REFUND rows are written by the order service, never requested by a client."""

    FULL = "FULL"
    DEPOSIT = "DEPOSIT"
    BALANCE = "BALANCE"
    REFUND = "REFUND"


# --- shared pieces -----------------------------------------------------------------------------


Lng = Annotated[float, Field(ge=-180, le=180)]
Lat = Annotated[float, Field(ge=-90, le=90)]


class GeoPoint(ApiModel):
    """A GeoJSON Point, the same JSON the catalog uses: `{"type": "Point", "coordinates": [lng, lat]}`."""

    type: Literal["Point"] = "Point"
    coordinates: tuple[Lng, Lat] = Field(examples=[[75.7873, 26.9124]])

    @classmethod
    def at(cls, lat: float, lng: float) -> "GeoPoint":
        return cls(coordinates=(lng, lat))

    @property
    def lng(self) -> float:
        return self.coordinates[0]

    @property
    def lat(self) -> float:
        return self.coordinates[1]


class DeliveryAddress(ApiModel):
    """Where the order is delivered. `location` is required: logistics pools drops that are close."""

    label: str | None = Field(default=None, examples=["Home"])
    line1: str = Field(examples=["12 Green Park"])
    line2: str | None = None
    city: str = Field(examples=["Jaipur"])
    pincode: str = Field(pattern=r"^\d{6}$", examples=["302001"])
    location: GeoPoint
    contact_phone: str | None = Field(default=None, examples=["+919999999999"])


class OrderItemIn(ApiModel):
    listing_id: UUID
    qty: float = Field(gt=0, examples=[2.5])


class OrderItem(ApiModel):
    """A line of an order. Name and price are copied from the listing at checkout time."""

    listing_id: UUID
    farm_id: UUID
    name: str = Field(examples=["Tomato"])
    unit: str = Field(examples=["kg"])
    qty: float
    unit_price: float
    line_total: float
    pickup_location: GeoPoint = Field(description="Where the farm hands the produce over")


class OrderEvent(ApiModel):
    """One entry of the status timeline (order_events)."""

    status: OrderStatus
    occurred_at: datetime
    note: str | None = None


# --- payments ----------------------------------------------------------------------------------


class PaymentIntentCreate(ApiModel):
    amount: float = Field(gt=0, examples=[499.0])
    kind: PaymentKind = PaymentKind.FULL
    # The reservation id for a DEPOSIT or BALANCE payment (V2); not needed for a FULL order payment.
    reference_id: UUID | None = None

    @field_validator("kind")
    @classmethod
    def _client_kinds(cls, value: PaymentKind) -> PaymentKind:
        if value == PaymentKind.REFUND:
            raise ValueError("REFUND payments are created by the order service")
        return value


class PaymentIntent(ApiModel):
    """A mock payment. In this project it always succeeds unless the amount is a test value."""

    id: UUID
    amount: float
    currency: str = "INR"
    kind: PaymentKind
    reference_id: UUID | None = None
    status: PaymentStatus
    created_at: datetime


# --- rating ------------------------------------------------------------------------------------


class OrderRatingCreate(ApiModel):
    # TODO(M1): the rating endpoint is assumed to live here, see docs/m1-assumptions.md.
    score: int = Field(ge=1, le=5, examples=[5])
    comment: str | None = Field(default=None, max_length=500)


class OrderRating(ApiModel):
    order_id: UUID
    score: int
    comment: str | None = None
    created_at: datetime


# --- orders ------------------------------------------------------------------------------------


class CheckoutRequest(ApiModel):
    """Body of POST /orders. The cart lives only in the app; items are sent here."""

    items: list[OrderItemIn] = Field(min_length=1)
    delivery_address: DeliveryAddress
    delivery_date: date | None = Field(
        default=None,
        description="Day the customer wants the delivery. Defaults to tomorrow. Orders due the "
        "same day are pooled into one route.",
    )
    payment_intent_id: UUID
    notes: str | None = Field(default=None, max_length=500)


class Order(ApiModel):
    """One order per farm, inside a checkout."""

    id: UUID
    checkout_id: UUID
    order_type: OrderType = OrderType.PRODUCE
    farm_id: UUID
    status: OrderStatus
    items: list[OrderItem]
    subtotal: float
    delivery_fee: float
    total: float
    currency: str = "INR"
    delivery_address: DeliveryAddress
    delivery_date: date
    created_at: datetime
    updated_at: datetime


class OrderDetail(Order):
    """GET /orders/{id}: the order plus its status timeline, oldest first."""

    events: list[OrderEvent]
    rating: OrderRating | None = None


class OrderSummary(ApiModel):
    """A row of GET /orders."""

    id: UUID
    checkout_id: UUID
    order_type: OrderType = OrderType.PRODUCE
    farm_id: UUID
    status: OrderStatus
    item_count: int
    total: float
    currency: str = "INR"
    created_at: datetime
    updated_at: datetime


class OrderList(ApiModel):
    """Cursor pagination, like the catalog: pass `next_cursor` back as `cursor` for the next page."""

    items: list[OrderSummary]
    next_cursor: str | None = None


class Checkout(ApiModel):
    """Result of POST /orders: one checkout holding one order per farm."""

    id: UUID
    customer_id: UUID
    payment_intent_id: UUID
    payment_status: PaymentStatus
    subtotal: float
    delivery_fee: float
    total: float
    currency: str = "INR"
    orders: list[Order]
    created_at: datetime


# --- errors (documentation only; the shape is produced by farm_common.errors) ------------------


class ErrorBody(ApiModel):
    code: str = Field(examples=["STOCK_UNAVAILABLE"])
    message: str
    correlation_id: str | None = None
    details: Any | None = None


class ErrorResponse(ApiModel):
    error: ErrorBody
