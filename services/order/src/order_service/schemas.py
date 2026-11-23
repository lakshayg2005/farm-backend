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

from pydantic import Field, field_validator, model_validator

from farm_common.schemas import ApiModel


class OrderStatus(StrEnum):
    """Lifecycle of a per-farm order (HLD section 11).

    PLACED -> CONFIRMED -> PACKED -> READY_FOR_DISPATCH, then logistics drives it: POOLED (pool
    assigned), IN_TRANSIT (picked up), DELIVERED, COMPLETED. There is no quality gate (removed in
    November 2026): packing makes the order ready for dispatch, and the customer's rating after
    delivery is the quality signal.
    """

    PLACED = "PLACED"
    CONFIRMED = "CONFIRMED"
    PACKED = "PACKED"
    READY_FOR_DISPATCH = "READY_FOR_DISPATCH"
    POOLED = "POOLED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class DeliveryMode(StrEnum):
    """How fast the customer wants the delivery.

    IMMEDIATE: the order is put on a route as soon as the farmer packs it (partners are offered it
    within seconds), at the normal delivery fee.
    FLEXIBLE: the customer accepts a later arrival. Packed orders wait, and once a day (10:00
    India time by default) logistics clusters every waiting FLEXIBLE order that is near each other
    into shared routes, so one delivery partner serves a whole neighbourhood. Cheaper delivery fee.
    """

    IMMEDIATE = "IMMEDIATE"
    FLEXIBLE = "FLEXIBLE"


class OrderType(StrEnum):
    """PRODUCE is the V1 purchase of listed produce. HARVEST is added by the V2 harvest flow."""

    PRODUCE = "PRODUCE"
    HARVEST = "HARVEST"


class HarvestOption(StrEnum):
    """What a customer does with a graded harvest (HLD section 12)."""

    DELIVERY = "DELIVERY"  # the harvest is delivered to the customer's address (pooled)
    BUYBACK = "BUYBACK"  # the farmer buys the harvest back and credits the customer


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class PaymentKind(StrEnum):
    """What a payment is for (HLD section 10). The land service pays reservation deposits through
    this API too. BALANCE and REFUND rows are written by the order service, never requested by a
    client."""

    FULL = "FULL"
    DEPOSIT = "DEPOSIT"
    BALANCE = "BALANCE"
    REFUND = "REFUND"
    # what the farmer earns from a completed order (HLD section 11, step 6); written by the
    # order service, never requested by a client
    PAYOUT = "PAYOUT"


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

    listing_id: UUID | None = Field(default=None, description="Empty on a harvest order")
    farm_id: UUID | None = Field(default=None, description="Empty on a harvest order")
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
    # The reservation id: required for a DEPOSIT, not used for a FULL order payment.
    reference_id: UUID | None = None

    @field_validator("kind")
    @classmethod
    def _client_kinds(cls, value: PaymentKind) -> PaymentKind:
        if value in (PaymentKind.REFUND, PaymentKind.BALANCE):
            raise ValueError(f"{value} payments are created by the order service")
        return value

    @model_validator(mode="after")
    def _deposit_needs_reservation(self) -> "PaymentIntentCreate":
        if self.kind == PaymentKind.DEPOSIT and self.reference_id is None:
            raise ValueError("referenceId (the reservation id) is required for a DEPOSIT")
        return self


class PaymentIntent(ApiModel):
    """A mock payment. In this project it always succeeds unless the amount is a test value.

    A FULL intent stays PENDING until it is used by POST /orders. A DEPOSIT is taken as soon as the
    intent is created, so `status` is already SUCCEEDED or FAILED in the response.
    """

    id: UUID
    amount: float
    currency: str = "INR"
    kind: PaymentKind
    reference_id: UUID | None = None
    status: PaymentStatus
    created_at: datetime


class PaymentWebhook(ApiModel):
    """Body of POST /orders/payments/webhook: the mock gateway reports the result of an intent."""

    payment_intent_id: UUID
    status: Literal["SUCCEEDED", "FAILED"]


class PaymentRecord(ApiModel):
    """A payment or refund that belongs to an order."""

    id: UUID
    kind: PaymentKind
    amount: float
    currency: str = "INR"
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
    delivery_mode: DeliveryMode = Field(
        default=DeliveryMode.IMMEDIATE,
        description="IMMEDIATE (default): on a route as soon as the farmer packs it. FLEXIBLE: the "
        "customer accepts a later arrival; the order joins the daily shared-delivery batch "
        "(see GET /orders/delivery-options) and has a lower delivery fee.",
    )
    payment_intent_id: UUID
    notes: str | None = Field(default=None, max_length=500)


class HarvestOrderChoice(ApiModel):
    """Body of POST /orders/harvest: what to do with the graded harvest of a reservation."""

    reservation_id: UUID
    option: HarvestOption
    delivery_address: DeliveryAddress | None = Field(
        default=None, description="Required for DELIVERY, ignored for BUYBACK"
    )
    delivery_date: date | None = Field(
        default=None, description="DELIVERY only. Defaults to tomorrow, like checkout."
    )
    delivery_mode: DeliveryMode = Field(
        default=DeliveryMode.IMMEDIATE,
        description="DELIVERY only: IMMEDIATE or FLEXIBLE, as at checkout.",
    )

    @model_validator(mode="after")
    def _delivery_needs_address(self) -> "HarvestOrderChoice":
        if self.option == HarvestOption.DELIVERY and self.delivery_address is None:
            raise ValueError("deliveryAddress is required for DELIVERY")
        return self


class Order(ApiModel):
    """One order per farm, inside a checkout. A HARVEST order is one order from one reservation."""

    id: UUID
    checkout_id: UUID
    order_type: OrderType = OrderType.PRODUCE
    farm_id: UUID | None = Field(default=None, description="Empty on a harvest order")
    reservation_id: UUID | None = Field(
        default=None, description="The land reservation a HARVEST order comes from"
    )
    harvest_option: HarvestOption | None = Field(
        default=None, description="HARVEST orders: empty until the customer chooses"
    )
    status: OrderStatus
    items: list[OrderItem]
    subtotal: float
    delivery_fee: float
    total: float
    currency: str = "INR"
    delivery_address: DeliveryAddress | None = Field(
        default=None, description="Empty on a harvest order that is not delivered"
    )
    delivery_date: date | None = None
    delivery_mode: DeliveryMode = DeliveryMode.IMMEDIATE
    batch_at: datetime | None = Field(
        default=None,
        description="FLEXIBLE orders not yet on a route: when the next shared-delivery batch that "
        "can take this order runs (an estimate; it is the first batch after the delivery date). "
        "Empty once the order is on a route, and for IMMEDIATE orders.",
    )
    created_at: datetime
    updated_at: datetime


class OrderDetail(Order):
    """GET /orders/{id}: the order plus its status timeline, oldest first."""

    events: list[OrderEvent]
    rating: OrderRating | None = None
    payments: list[PaymentRecord] = Field(
        description="Payments and refunds of this order, oldest first"
    )
    balance_due: float = Field(
        default=0,
        description="HARVEST DELIVERY: what is still to be paid, settled when the order completes",
    )


class OrderSummary(ApiModel):
    """A row of GET /orders."""

    id: UUID
    checkout_id: UUID
    order_type: OrderType = OrderType.PRODUCE
    farm_id: UUID | None = None
    reservation_id: UUID | None = None
    harvest_option: HarvestOption | None = None
    delivery_mode: DeliveryMode = DeliveryMode.IMMEDIATE
    batch_at: datetime | None = None
    status: OrderStatus
    item_count: int
    total: float
    currency: str = "INR"
    rating_score: int | None = Field(
        default=None, description="The customer's score (1-5) once a completed order is rated"
    )
    created_at: datetime
    updated_at: datetime


class DeliveryOption(ApiModel):
    mode: DeliveryMode
    title: str
    description: str
    fee_per_order: float = Field(description="Delivery fee charged for each farm's order")


class DeliveryOptions(ApiModel):
    """GET /orders/delivery-options: what the customer can choose at checkout (one source for the
    website and the apps, so fees and the batch time are never hard-coded in a client)."""

    options: list[DeliveryOption]
    batch_time: str = Field(description="Local time of the daily shared-delivery batch, HH:MM")
    timezone: str = "Asia/Kolkata"
    next_batch_at: datetime = Field(description="The next batch run from now")


class Payout(ApiModel):
    order_id: UUID
    amount: float
    currency: str = "INR"
    paid_at: datetime


class PayoutList(ApiModel):
    """A farmer's payouts, newest first, and their sum."""

    items: list[Payout]
    total: float


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


class CancelRequest(ApiModel):
    reason: str | None = Field(default=None, max_length=500, examples=["Ordered by mistake"])
