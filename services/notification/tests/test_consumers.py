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


def test_a_harvest_order_asks_the_customer_to_choose_and_spares_the_farmer():
    cust_id, farmer_id = uuid4(), uuid4()
    event = Event(
        type="order.placed",
        producer="order",
        payload={
            "orderId": str(uuid4()),
            "customerId": str(cust_id),
            "farmOwnerId": str(farmer_id),
            "orderType": "HARVEST",
            "reservationId": str(uuid4()),
            "items": [{"name": "Harvest: Wheat", "qty": 92.0}],
            "total": 2760.0,
        },
    )

    user_id, ntype, title, body, payload = map_event_to_notification(event)

    assert (user_id, ntype, title) == (
        cust_id,
        NotificationType.HARVEST_READY,
        "Your harvest is ready",
    )
    assert "Wheat has been harvested (92 kg)" in body and "delivery or buy-back" in body
    assert payload["reservationId"] == event.payload["reservationId"]
    assert map_event_to_farmer_notification(event) is None  # nothing for the farmer to pack


def test_packing_a_produce_order_sends_one_message():
    base = {"orderId": str(uuid4()), "customerId": str(uuid4()), "orderType": "PRODUCE"}

    packed = map_event_to_notification(Event(type="order.packed", producer="order", payload=base))
    ready = map_event_to_notification(
        Event(type="order.ready_for_dispatch", producer="order", payload=base)
    )

    assert packed is not None and "waiting for a delivery partner" in packed[3]
    assert ready is None


def test_a_packed_flexible_order_says_it_leaves_with_the_shared_batch():
    payload = {"orderId": str(uuid4()), "customerId": str(uuid4()), "deliveryMode": "FLEXIBLE"}
    packed = map_event_to_notification(
        Event(type="order.packed", producer="order", payload=payload)
    )
    assert packed is not None and "shared delivery" in packed[3] and "next batch" in packed[3]


def land(event_type: str, **extra) -> Event:
    return Event(
        type=event_type,
        producer="land",
        payload={
            "reservationId": str(uuid4()),
            "customerId": str(CUSTOMER),
            "farmerId": str(FARMER),
            "cropId": "green-chilli",
            **extra,
        },
    )


CUSTOMER, FARMER = uuid4(), uuid4()


def test_land_requests_reach_the_right_side():
    requested = land(
        "reservation.requested", plotTitle="River plot", season="Rabi", expectedQtyKg=400
    )
    rejected = land("reservation.rejected")
    activated = land("reservation.activated")
    by_customer = land("reservation.cancelled", cancelledBy="customer")
    by_farmer = land("reservation.cancelled", cancelledBy="farmer")
    expired = land("reservation.expired")

    to_farmer = map_event_to_farmer_notification(requested)
    assert to_farmer[0] == FARMER and to_farmer[1] == NotificationType.RESERVATION_REQUESTED
    assert "green chilli (about 400 kg) on River plot, Rabi season" in to_farmer[3]
    assert map_event_to_notification(requested) is None

    assert map_event_to_notification(rejected)[:2] == (
        CUSTOMER,
        NotificationType.RESERVATION_REJECTED,
    )
    assert map_event_to_farmer_notification(activated)[:2] == (
        FARMER,
        NotificationType.RESERVATION_ACTIVATED,
    )
    assert map_event_to_farmer_notification(by_customer)[0] == FARMER
    assert map_event_to_notification(by_customer) is None
    assert map_event_to_notification(by_farmer)[0] == CUSTOMER
    assert map_event_to_farmer_notification(by_farmer) is None
    assert map_event_to_notification(expired)[0] == CUSTOMER  # both sides hear about it
    assert map_event_to_farmer_notification(expired)[0] == FARMER


def test_a_crop_update_reaches_the_customer():
    event = land("crop.updated", stage="FLOWERING", note="Buds everywhere.", photoCount=2)

    user_id, ntype, title, body, payload = map_event_to_notification(event)

    assert (user_id, ntype) == (CUSTOMER, NotificationType.CROP_UPDATE)
    assert title == "Your green chilli: flowering"
    assert body == "The farmer posted a crop update (2 photos). Buds everywhere."
    assert payload["stage"] == "FLOWERING"


def test_a_verified_farmer_is_told():
    farmer = uuid4()
    event = Event(type="farmer.verified", producer="identity", payload={"userId": str(farmer)})

    user_id, ntype, title, _, _ = map_event_to_farmer_notification(event)

    assert (user_id, ntype, title) == (farmer, NotificationType.FARMER_VERIFIED, "You are verified")


def test_a_buyback_tells_the_farmer_the_produce_is_theirs():
    event = Event(
        type="order.completed",
        producer="order",
        payload={
            "orderId": str(uuid4()),
            "customerId": str(uuid4()),
            "farmOwnerId": str(FARMER),
            "orderType": "HARVEST",
            "harvestOption": "BUYBACK",
        },
    )

    assert map_event_to_farmer_notification(event)[2] == "Harvest bought back"


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


def test_route_offer_goes_to_every_offered_partner_and_taken_replaces_it():
    from notification_service.consumers import PUSH_ONLY, map_event_to_partner_notifications

    a, b, pool = uuid4(), uuid4(), str(uuid4())
    offered = map_event_to_partner_notifications(
        Event(
            type="pool.offered",
            producer="logistics",
            payload={
                "poolId": pool,
                "poolDate": "2026-11-24",
                "stopCount": 4,
                "totalKm": 12.5,
                "firstPickup": {"lat": 21.0, "lng": 81.0, "address": "Patan farm"},
                "offers": [
                    {"partnerId": str(a), "distanceKm": 1.24},
                    {"partnerId": str(b), "distanceKm": None},
                ],
            },
        )
    )
    assert [o[0] for o in offered] == [a, b]
    assert all(o[1] == NotificationType.POOL_OFFER for o in offered)
    assert "1.2 km away" in offered[0][3] and "Patan farm" in offered[0][3]
    assert "near you" in offered[1][3]
    assert offered[0][4]["tag"] == f"pool-{pool}" and offered[0][4]["poolId"] == pool

    taken = map_event_to_partner_notifications(
        Event(
            type="pool.offer_closed",
            producer="logistics",
            payload={"poolId": pool, "partnerIds": [str(b)]},
        )
    )
    assert [(t[0], t[1]) for t in taken] == [(b, NotificationType.POOL_OFFER_TAKEN)]
    assert taken[0][4]["tag"] == f"pool-{pool}"  # same tag: replaces the offer on the phone
    assert NotificationType.POOL_OFFER_TAKEN in PUSH_ONLY  # not kept in the inbox
    assert (
        map_event_to_partner_notifications(
            Event(type="pool.assigned", producer="logistics", payload={})
        )
        == []
    )
