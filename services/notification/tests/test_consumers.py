"""Tests for notification service event consumers and push dispatch."""

from __future__ import annotations

from uuid import uuid4

from farm_common.events import Event
from notification_service.consumers import map_event_to_notification
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
