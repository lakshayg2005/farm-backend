"""Events published and consumed by the order service. Payloads are camelCase, like every API.

Published (through the outbox, in the same transaction as the status change):
    order.placed              checkout succeeded and payment was taken (status CONFIRMED); also a
                              HARVEST order created when the farmer marks the harvest ready
                              (status PLACED, the customer then picks delivery or buy-back)
    order.packed              the farmer packed it
    order.ready_for_dispatch  packed (or a harvest the customer wants delivered); logistics can
                              pool it. An IMMEDIATE order is pooled at once, a FLEXIBLE one waits
                              for the daily shared-delivery batch (`deliveryMode` in the payload)
    order.pooled              logistics put it on a delivery route (notification tells the customer)
    order.in_transit          the partner picked it up
    order.completed           delivered and closed
    order.rated               the customer rated a completed order (only orders that have a farm);
                              catalog keeps each farm's average from these
    payment.succeeded         a payment was taken: a checkout (FULL), a reservation deposit (DEPOSIT,
                              `referenceId` = reservation id; land activates the reservation) or the
                              balance of a harvest order (BALANCE)
    order.cancelled           cancelled by the customer, or payment failed; catalog releases the
                              stock reserved for `orderId` when it sees this

Consumed:
    reservation.accepted -> remembers the reservation (customer, farmer, deposit, agreed price)
    harvest.ready       -> creates the HARVEST order from the harvested weight; the plot is its
                           pickup point
    reservation.cancelled -> refunds a paid deposit (TODO(M1): event name assumed, owner M7)
    pool.assigned       -> POOLED                      (TODO(M1): payload assumed, owner M2)
    delivery.started    -> IN_TRANSIT                  (TODO(M1): event name assumed, owner M2)
    delivery.completed  -> DELIVERED, then COMPLETED

Payload shapes are the models below. JSON Schemas generated from them are committed in
contracts/events/ (regenerate with services/order/scripts/export_events.py).
"""

from datetime import date
from typing import Any, Literal
from uuid import UUID

from farm_common.schemas import ApiModel
from order_service.db import OrderRow
from order_service.schemas import (
    DeliveryAddress,
    DeliveryMode,
    GeoPoint,
    HarvestOption,
    OrderStatus,
    OrderType,
    PaymentKind,
)

PRODUCER = "order"

ORDER_PLACED = "order.placed"
ORDER_PACKED = "order.packed"
ORDER_READY_FOR_DISPATCH = "order.ready_for_dispatch"
ORDER_POOLED = "order.pooled"
ORDER_IN_TRANSIT = "order.in_transit"
ORDER_COMPLETED = "order.completed"
ORDER_CANCELLED = "order.cancelled"
ORDER_RATED = "order.rated"
PAYMENT_SUCCEEDED = "payment.succeeded"

RESERVATION_ACCEPTED = "reservation.accepted"
RESERVATION_CANCELLED = "reservation.cancelled"
HARVEST_READY = "harvest.ready"
POOL_ASSIGNED = "pool.assigned"
DELIVERY_STARTED = "delivery.started"
DELIVERY_COMPLETED = "delivery.completed"

CONSUMED = [
    RESERVATION_ACCEPTED,
    RESERVATION_CANCELLED,
    HARVEST_READY,
    POOL_ASSIGNED,
    DELIVERY_STARTED,
    DELIVERY_COMPLETED,
]


class EventItem(ApiModel):
    listing_id: UUID | None = None  # empty on a harvest order
    name: str
    unit: str
    qty: float
    unit_price: float
    pickup_location: GeoPoint


class OrderEventPayload(ApiModel):
    """Payload shared by every order.* event: enough for notification, land and logistics."""

    order_id: UUID
    checkout_id: UUID
    customer_id: UUID
    farm_id: UUID | None = None  # empty on a harvest order
    farm_owner_id: UUID | None = None
    order_type: OrderType
    reservation_id: UUID | None = None  # HARVEST orders
    harvest_option: HarvestOption | None = None  # HARVEST orders, once the customer chose
    status: OrderStatus
    items: list[EventItem]
    subtotal: float
    delivery_fee: float
    total: float
    currency: str
    delivery_address: DeliveryAddress | None = None  # empty unless the order is delivered
    delivery_date: date | None = None
    # FLEXIBLE orders are not routed when packed: logistics holds them for the daily batch
    delivery_mode: DeliveryMode = DeliveryMode.IMMEDIATE


class OrderCancelledPayload(OrderEventPayload):
    reason: str
    cancelled_by: Literal["customer", "farmer", "admin", "system"]
    refund_amount: float


class OrderRatedPayload(ApiModel):
    """A customer rated a completed order. Catalog adds `score` to the farm's average."""

    order_id: UUID
    farm_id: UUID
    farm_owner_id: UUID | None = None
    customer_id: UUID
    score: int
    comment: str | None = None


class PaymentSucceededPayload(ApiModel):
    """A payment went through. Land (#18) activates a reservation when it sees a DEPOSIT with its
    id as `referenceId`. A BALANCE payment also lists the harvest order it settled."""

    payment_id: UUID
    kind: PaymentKind
    amount: float
    currency: str
    customer_id: UUID
    reference_id: UUID | None = None
    checkout_id: UUID | None = None
    order_ids: list[UUID] = []


# event type -> payload model; used to generate contracts/events/*.json
PUBLISHED: dict[str, type[ApiModel]] = {
    ORDER_PLACED: OrderEventPayload,
    ORDER_PACKED: OrderEventPayload,
    ORDER_READY_FOR_DISPATCH: OrderEventPayload,
    ORDER_POOLED: OrderEventPayload,
    ORDER_IN_TRANSIT: OrderEventPayload,
    ORDER_COMPLETED: OrderEventPayload,
    ORDER_CANCELLED: OrderCancelledPayload,
    ORDER_RATED: OrderRatedPayload,
    PAYMENT_SUCCEEDED: PaymentSucceededPayload,
}


def build_payload(order: OrderRow, **extra: Any) -> dict[str, Any]:
    """camelCase payload for an order.* event from a loaded order (with items)."""
    base = {
        "order_id": order.id,
        "checkout_id": order.checkout_id,
        "customer_id": order.customer_id,
        "farm_id": order.farm_id,
        "farm_owner_id": order.farm_owner_id,
        "order_type": order.order_type,
        "reservation_id": order.reservation_id,
        "harvest_option": order.harvest_option,
        "status": order.status,
        "items": [
            {
                "listing_id": i.listing_id,
                "name": i.name,
                "unit": i.unit,
                "qty": float(i.quantity),
                "unit_price": float(i.unit_price),
                "pickup_location": i.pickup_location,
            }
            for i in order.items
        ],
        "subtotal": float(order.subtotal),
        "delivery_fee": float(order.delivery_fee),
        "total": float(order.total),
        "currency": order.currency,
        "delivery_address": order.delivery_address,
        "delivery_date": order.delivery_date,
        "delivery_mode": order.delivery_mode or "IMMEDIATE",
        **extra,
    }
    model = OrderCancelledPayload if "reason" in extra else OrderEventPayload
    return model.model_validate(base).model_dump(mode="json", by_alias=True)


def json_schema(event_type: str) -> dict[str, Any]:
    """JSON Schema of an event's payload, as committed in contracts/events/<event_type>.json."""
    schema = PUBLISHED[event_type].model_json_schema()
    schema["title"] = event_type
    schema["description"] = f"Payload of the {event_type} event (produced by the order service)."
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
