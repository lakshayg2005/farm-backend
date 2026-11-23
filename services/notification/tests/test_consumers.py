"""Tests for notification service event consumers and push dispatch."""

from __future__ import annotations

from uuid import uuid4

from farm_common.events import Event
from notification_service.consumers import (
    map_event_to_farmer_notification,
    map_event_to_notification,
)
from notification_service.schemas import NotificationType


def test_map_order_placed_event():
    cust_id = uuid4()
    order_id = uuid4()
    event = Event(
        type="order.placed",
        producer="order",
        payload={
            "customerId": str(cust_id),
            "orderId": str(order_id),
            "total": 520.50,
        },
    )
    mapped = map_event_to_notification(event)
    assert mapped is not None
    user_id, ntype, title, body, payload = mapped
    assert user_id == cust_id
    assert ntype == NotificationType.ORDER_PLACED
    assert "Placed" in title
    assert str(order_id)[:8] in body
    assert payload["total"] == 520.50


def test_map_payment_succeeded_event():
    cust_id = uuid4()
    event = Event(
        type="payment.succeeded",
        producer="order",
        payload={
            "customerId": str(cust_id),
            "paymentId": str(uuid4()),
            "amount": 1200.0,
        },
    )
    mapped = map_event_to_notification(event)
    assert mapped is not None
    user_id, ntype, title, body, payload = mapped
    assert user_id == cust_id
    assert ntype == NotificationType.PAYMENT_SUCCEEDED
    assert "₹1200.00" in body


def test_map_reservation_accepted_event():
    cust_id = uuid4()
    res_id = uuid4()
    event = Event(
        type="reservation.accepted",
        producer="land",
        payload={
            "customerId": str(cust_id),
            "farmerId": str(uuid4()),
            "reservationId": str(res_id),
            "deposit": 300.0,
        },
    )
    mapped = map_event_to_notification(event)
    assert mapped is not None
    user_id, ntype, title, body, _ = mapped
    assert user_id == cust_id
    assert ntype == NotificationType.RESERVATION_ACCEPTED
    assert "₹300.00" in body


def test_map_harvest_ready_event():
    cust_id = uuid4()
    event = Event(
        type="harvest.ready",
        producer="land",
        payload={
            "customerId": str(cust_id),
            "cropId": "organic-wheat",
            "reservationId": str(uuid4()),
        },
    )
    mapped = map_event_to_notification(event)
    assert mapped is not None
    user_id, ntype, title, body, _ = mapped
    assert user_id == cust_id
    assert ntype == NotificationType.HARVEST_READY
    assert "organic-wheat" in body


def test_map_quality_graded_event():
    cust_id = uuid4()
    event = Event(
        type="quality.graded",
        producer="quality",
        payload={
            "customerId": str(cust_id),
            "subjectType": "ORDER",
            "grade": "A",
            "result": "PASS",
            "inspectionId": str(uuid4()),
        },
    )
    mapped = map_event_to_notification(event)
    assert mapped is not None
    user_id, ntype, title, body, _ = mapped
    assert user_id == cust_id
    assert ntype == NotificationType.ORDER_QC_PASSED
    assert "Passed" in title
    assert "Grade A" in body


def test_map_unknown_or_missing_customer_returns_none():
    event = Event(type="unknown.event", producer="test", payload={})
    assert map_event_to_notification(event) is None

    event_no_cust = Event(type="order.placed", producer="order", payload={"orderId": "123"})
    assert map_event_to_notification(event_no_cust) is None


def _order_event(etype: str, **payload):
    return Event(
        type=etype, producer="order", payload={"orderId": str(uuid4()), "total": 120.0, **payload}
    )


def test_farmer_is_told_about_a_new_paid_order():
    owner, customer = uuid4(), uuid4()
    event = _order_event("order.placed", customerId=str(customer), farmOwnerId=str(owner))
    farmer = map_event_to_farmer_notification(event)
    assert farmer is not None
    user_id, ntype, title, body, payload = farmer
    assert user_id == owner
    assert ntype == NotificationType.ORDER_PLACED
    assert title == "New order to pack"
    assert "₹120.00" in body
    assert payload["orderId"] == event.payload["orderId"]
    # the customer still gets their own notification from the same event
    assert map_event_to_notification(event)[0] == customer


def test_farmer_is_told_when_an_order_is_cancelled_or_completed():
    owner = str(uuid4())
    cancelled = map_event_to_farmer_notification(_order_event("order.cancelled", farmOwnerId=owner))
    done = map_event_to_farmer_notification(_order_event("order.completed", farmOwnerId=owner))
    assert cancelled[1] == NotificationType.ORDER_CANCELLED
    assert done[1] == NotificationType.ORDER_COMPLETED
    assert "payout" in done[3]


def test_no_farmer_notification_without_an_owner_or_for_other_events():
    assert (
        map_event_to_farmer_notification(_order_event("order.placed", customerId=str(uuid4())))
        is None
    )
    assert (
        map_event_to_farmer_notification(_order_event("order.packed", farmOwnerId=str(uuid4())))
        is None
    )


def _stock_event(**payload):
    base = {
        "listingId": str(uuid4()),
        "farmerId": str(uuid4()),
        "cropName": "Tomato",
        "variety": None,
        "qtyAvailable": 8,
        "unit": "kg",
        "soldOut": False,
    }
    return {"type": "listing.stock_low", "payload": {**base, **payload}}


def test_farmer_is_warned_when_stock_runs_low():
    event = _stock_event()
    user_id, ntype, title, body, data = map_event_to_farmer_notification(event)
    assert str(user_id) == event["payload"]["farmerId"]
    assert ntype == NotificationType.LOW_STOCK
    assert title == "Low stock: Tomato"
    assert "Only 8 kg" in body
    assert data["soldOut"] is False


def test_farmer_is_told_when_a_listing_sells_out():
    event = _stock_event(qtyAvailable=0, soldOut=True, variety="Hybrid")
    _, _, title, body, data = map_event_to_farmer_notification(event)
    assert title == "Tomato (Hybrid) is sold out"
    assert "Add stock" in body
    assert data["soldOut"] is True


def test_low_stock_without_a_farmer_is_ignored():
    assert map_event_to_farmer_notification(_stock_event(farmerId=None)) is None
    # and it never produces a customer notification
    assert map_event_to_notification(_stock_event()) is None


def test_farmer_is_told_about_a_new_rating():
    owner = str(uuid4())
    event = {
        "type": "order.rated",
        "payload": {"orderId": str(uuid4()), "farmOwnerId": owner, "score": 4, "comment": "Fresh"},
    }
    user_id, ntype, title, body, data = map_event_to_farmer_notification(event)
    assert str(user_id) == owner
    assert ntype == NotificationType.RATING_RECEIVED
    assert title == "New rating: 4 of 5"
    assert "Fresh" in body and data["score"] == 4
    # the customer who rated is not notified about their own rating
    assert map_event_to_notification(event) is None


def test_order_pooled_tells_the_customer_and_pool_assigned_the_partner():
    cust, oid, partner = uuid4(), uuid4(), uuid4()
    mapped = map_event_to_notification(
        Event(
            type="order.pooled",
            producer="order",
            payload={"customerId": str(cust), "orderId": str(oid)},
        )
    )
    assert mapped is not None
    assert mapped[0] == cust and mapped[2] == "On a delivery route"
    assert mapped[4]["orderId"] == str(oid)

    route = map_event_to_notification(
        Event(
            type="pool.assigned",
            producer="logistics",
            payload={
                "partnerId": str(partner),
                "poolId": str(uuid4()),
                "poolDate": "2026-11-24",
                "stopCount": 4,
                "totalKm": None,
            },
        )
    )
    assert route is not None
    assert route[0] == partner and route[2] == "New route for 2026-11-24"
