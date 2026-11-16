"""Canned example data returned by the contract stub routes.

TODO(M1): delete this module when the real handlers land in #13; nothing else imports it.
"""

from datetime import UTC, date, datetime
from uuid import UUID

from order_service.schemas import (
    Checkout,
    DeliveryAddress,
    GeoPoint,
    Order,
    OrderDetail,
    OrderEvent,
    OrderItem,
    OrderList,
    OrderRating,
    OrderStatus,
    OrderSummary,
    PaymentIntent,
    PaymentKind,
    PaymentStatus,
)

CHECKOUT_ID = UUID("0b6f1c2e-5d1a-4c55-9a3e-7f1d2c3b4a51")
CUSTOMER_ID = UUID("7c1d9f0a-2b34-4e6f-8a90-1b2c3d4e5f60")
PAYMENT_INTENT_ID = UUID("3e8a4b6c-9d12-4f7a-b1c3-5d6e7f8a9b01")
ORDER_ID = UUID("a1f0c9d8-7e6b-4a5c-9d4e-3f2a1b0c9d8e")
ORDER_ID_2 = UUID("b2e1dae9-8f7c-4b6d-ae5f-4a3b2c1d0e9f")
FARM_ID = UUID("c3d2ebfa-9a8d-4c7e-bf60-5b4c3d2e1f0a")
FARM_ID_2 = UUID("d4c3fc0b-ab9e-4d8f-c071-6c5d4e3f2a1b")
LISTING_ID = UUID("e5b40d1c-bcaf-4e90-d182-7d6e5f4a3b2c")
LISTING_ID_2 = UUID("f6a51e2d-cdb0-4fa1-e293-8e7f6a5b4c3d")
NOW = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)

ADDRESS = DeliveryAddress(
    label="Home",
    line1="12 Green Park",
    city="Jaipur",
    pincode="302001",
    location=GeoPoint.at(26.9124, 75.7873),
    contact_phone="+919999999999",
)


def _order(order_id: UUID, farm_id: UUID, listing_id: UUID, name: str, price: float) -> Order:
    return Order(
        id=order_id,
        checkout_id=CHECKOUT_ID,
        farm_id=farm_id,
        status=OrderStatus.CONFIRMED,
        items=[
            OrderItem(
                listing_id=listing_id,
                farm_id=farm_id,
                name=name,
                unit="kg",
                qty=2.0,
                unit_price=price,
                line_total=round(2.0 * price, 2),
                pickup_location=GeoPoint.at(26.85, 75.8),
            )
        ],
        subtotal=round(2.0 * price, 2),
        delivery_fee=20.0,
        total=round(2.0 * price + 20.0, 2),
        delivery_address=ADDRESS,
        delivery_date=date(2026, 11, 16),
        created_at=NOW,
        updated_at=NOW,
    )


def sample_orders() -> list[Order]:
    return [
        _order(ORDER_ID, FARM_ID, LISTING_ID, "Tomato", 40.0),
        _order(ORDER_ID_2, FARM_ID_2, LISTING_ID_2, "Spinach", 25.0),
    ]


def sample_checkout() -> Checkout:
    orders = sample_orders()
    return Checkout(
        id=CHECKOUT_ID,
        customer_id=CUSTOMER_ID,
        payment_intent_id=PAYMENT_INTENT_ID,
        payment_status=PaymentStatus.SUCCEEDED,
        subtotal=sum(o.subtotal for o in orders),
        delivery_fee=sum(o.delivery_fee for o in orders),
        total=sum(o.total for o in orders),
        orders=orders,
        created_at=NOW,
    )


def sample_order_list() -> OrderList:
    summaries = [
        OrderSummary(
            id=o.id,
            checkout_id=o.checkout_id,
            farm_id=o.farm_id,
            status=o.status,
            item_count=len(o.items),
            total=o.total,
            created_at=o.created_at,
            updated_at=o.updated_at,
        )
        for o in sample_orders()
    ]
    return OrderList(items=summaries, next_cursor=None)


def sample_order_detail(order_id: UUID) -> OrderDetail:
    order = sample_orders()[0].model_copy(update={"id": order_id})
    return OrderDetail(
        **order.model_dump(),
        events=[
            OrderEvent(status=OrderStatus.PLACED, occurred_at=NOW),
            OrderEvent(status=OrderStatus.CONFIRMED, occurred_at=NOW, note="Payment received"),
        ],
    )


def sample_payment_intent(amount: float, kind: PaymentKind, reference_id: UUID | None):
    return PaymentIntent(
        id=PAYMENT_INTENT_ID,
        amount=amount,
        kind=kind,
        reference_id=reference_id,
        status=PaymentStatus.PENDING,
        created_at=NOW,
    )


def sample_rating(order_id: UUID, score: int, comment: str | None) -> OrderRating:
    return OrderRating(order_id=order_id, score=score, comment=comment, created_at=NOW)
