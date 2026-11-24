from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from farm_common.auth import Role
from farm_common.errors import Conflict
from farm_common.events import Event
from order_service import consumers, service
from order_service import events as ev
from order_service.db import OrderRow
from order_service.events import OrderCancelledPayload, OrderEventPayload

from .conftest import (
    CUSTOMER,
    FARM_A,
    FARM_B,
    FARMER_A,
    FARMER_B,
    OTHER_CUSTOMER,
    SPINACH,
    TOMATO,
    user,
)

FARMER = user(FARMER_A, Role.FARMER)
ADMIN = user(uuid4(), Role.ADMIN)


async def deliver(api, event_type: str, **payload) -> Event:
    event = Event(type=event_type, producer="test", payload=payload)
    await consumers.handle_event(api.session_factory, event)
    return event


async def status_of(api, order_id: str) -> str:
    return (await api.http.get(f"/orders/{order_id}")).json()["status"]


async def pack(api, order_id: str):
    api.login(FARMER)
    response = await api.http.post(f"/orders/{order_id}/pack")
    api.login(user(CUSTOMER, Role.CUSTOMER))
    return response


# --- packing -----------------------------------------------------------------------------------


async def test_farmer_packs_own_order_and_it_is_ready_for_dispatch(api):
    order = await api.placed_order(FARM_A)

    response = await pack(api, order["id"])

    assert response.status_code == 200
    assert response.json()["status"] == "READY_FOR_DISPATCH"
    assert [e["status"] for e in response.json()["events"]] == [
        "PLACED",
        "CONFIRMED",
        "PACKED",
        "READY_FOR_DISPATCH",
    ]
    (packed,) = await api.outbox("order.packed")
    payload = OrderEventPayload.model_validate(packed.payload)
    assert str(payload.order_id) == order["id"]
    assert payload.status == "PACKED"
    assert payload.items[0].name == "Tomato"
    (ready,) = await api.outbox("order.ready_for_dispatch")
    assert ready.payload["orderId"] == order["id"]
    assert ready.payload["status"] == "READY_FOR_DISPATCH"


async def test_farmer_cannot_pack_another_farms_order(api):
    order = await api.placed_order(FARM_A)
    api.login(user(FARMER_B, Role.FARMER))

    response = await api.http.post(f"/orders/{order['id']}/pack")

    assert response.status_code == 403


async def test_customer_cannot_pack(api):
    order = await api.placed_order()

    assert (await api.http.post(f"/orders/{order['id']}/pack")).status_code == 403


async def test_packing_twice_is_rejected(api):
    order = await api.placed_order()
    await pack(api, order["id"])

    response = await pack(api, order["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"


# --- delivery events ---------------------------------------------------------------------------


async def test_a_packed_order_can_no_longer_be_cancelled(api):
    order = await api.placed_order()
    await pack(api, order["id"])

    response = await api.http.post(f"/orders/{order['id']}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_NOT_CANCELLABLE"


async def _dispatchable(api) -> str:
    order = await api.placed_order()
    await pack(api, order["id"])
    return order["id"]


async def test_pool_started_and_completed_follow_the_hld_statuses(api):
    order_id = await _dispatchable(api)

    await deliver(api, "pool.assigned", orderIds=[order_id])
    assert await status_of(api, order_id) == "POOLED"
    await deliver(api, "delivery.started", orderId=order_id)
    assert await status_of(api, order_id) == "IN_TRANSIT"
    await deliver(api, "delivery.completed", orderId=order_id)

    detail = (await api.http.get(f"/orders/{order_id}")).json()
    assert detail["status"] == "COMPLETED"
    assert [e["status"] for e in detail["events"]][-5:] == [
        "READY_FOR_DISPATCH",
        "POOLED",
        "IN_TRANSIT",
        "DELIVERED",
        "COMPLETED",
    ]
    (event,) = await api.outbox("order.completed")
    assert event.payload["status"] == "COMPLETED"


async def test_delivery_completed_without_earlier_events_still_completes(api):
    order_id = await _dispatchable(api)

    await deliver(api, "delivery.completed", orderIds=[order_id])

    assert await status_of(api, order_id) == "COMPLETED"


async def test_delivery_events_for_orders_not_ready_are_ignored(api):
    order = await api.placed_order()

    await deliver(api, "delivery.completed", orderId=order["id"])

    assert await status_of(api, order["id"]) == "CONFIRMED"


async def test_unrelated_events_are_ignored(api):
    await deliver(api, "user.registered", user_id=str(uuid4()))


# --- cancel ------------------------------------------------------------------------------------


async def test_customer_cancels_and_cancelled_event_carries_items_for_stock_release(api):
    order = await api.placed_order(FARM_A)

    response = await api.http.post(
        f"/orders/{order['id']}/cancel", json={"reason": "Changed my mind"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["events"][-1]["note"] == "Changed my mind"
    (event,) = await api.outbox("order.cancelled")
    payload = OrderCancelledPayload.model_validate(event.payload)
    assert payload.cancelled_by == "customer"
    assert payload.refund_amount == order["total"]
    assert [(str(i.listing_id), i.qty) for i in payload.items] == [(str(TOMATO), 2.0)]


async def test_cancel_without_a_body(api):
    order = await api.placed_order()

    response = await api.http.post(f"/orders/{order['id']}/cancel")

    assert response.status_code == 200
    (event,) = await api.outbox("order.cancelled")
    assert event.payload["reason"] == "CUSTOMER_REQUEST"


async def test_customer_cannot_cancel_after_dispatch(api):
    order_id = await _dispatchable(api)

    response = await api.http.post(f"/orders/{order_id}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_NOT_CANCELLABLE"


async def test_admin_can_cancel_a_packed_order_waiting_for_dispatch(api):
    order_id = await _dispatchable(api)
    api.login(ADMIN)

    response = await api.http.post(f"/orders/{order_id}/cancel", json={"reason": "Farm flooded"})

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    (event,) = await api.outbox("order.cancelled")
    assert event.payload["cancelledBy"] == "admin"
    assert event.payload["reason"] == "Farm flooded"


async def test_admin_can_cancel_an_order_on_a_route_but_not_one_in_transit(api):
    order_id = await _dispatchable(api)
    await deliver(api, "pool.assigned", orderIds=[order_id])
    response = await api.checkout(key="checkout-0002")
    in_transit = response.json()["orders"][0]["id"]
    await pack(api, in_transit)
    await deliver(api, "delivery.started", orderId=in_transit)
    api.login(ADMIN)

    assert (await api.http.post(f"/orders/{order_id}/cancel")).status_code == 200
    assert await status_of(api, order_id) == "CANCELLED"
    response = await api.http.post(f"/orders/{in_transit}/cancel")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_NOT_CANCELLABLE"


async def test_cannot_cancel_twice(api):
    order = await api.placed_order()
    await api.http.post(f"/orders/{order['id']}/cancel")

    assert (await api.http.post(f"/orders/{order['id']}/cancel")).status_code == 409
    assert len(await api.outbox("order.cancelled")) == 1


async def test_cannot_cancel_someone_elses_order(api):
    order = await api.placed_order()
    api.login(user(OTHER_CUSTOMER, Role.CUSTOMER))

    assert (await api.http.post(f"/orders/{order['id']}/cancel")).status_code == 404


async def test_admin_can_cancel(api):
    order = await api.placed_order()
    api.login(ADMIN)

    response = await api.http.post(f"/orders/{order['id']}/cancel")

    assert response.status_code == 200
    (event,) = await api.outbox("order.cancelled")
    assert event.payload["cancelledBy"] == "admin"


# --- guarded updates ---------------------------------------------------------------------------


async def test_status_update_is_guarded_against_a_concurrent_change(api):
    order = await api.placed_order()
    async with api.session_factory() as session:
        stale = await service._load_order(session, UUID(order["id"]))
        # someone else cancels the order after we loaded it
        async with api.session_factory() as other, other.begin():
            await other.execute(
                update(OrderRow).where(OrderRow.id == stale.id).values(status="CANCELLED")
            )

        with pytest.raises(Conflict) as error:
            await service._transition(session, stale, service.OrderStatus.PACKED, ev.ORDER_PACKED)

    assert error.value.code == "ORDER_CHANGED"


# --- rating ------------------------------------------------------------------------------------


async def test_rating_a_completed_order(api):
    order_id = await _dispatchable(api)
    await deliver(api, "delivery.completed", orderId=order_id)

    response = await api.http.post(
        f"/orders/{order_id}/rating", json={"score": 5, "comment": "Fresh"}
    )

    assert response.status_code == 201
    assert response.json()["score"] == 5
    detail = (await api.http.get(f"/orders/{order_id}")).json()
    assert detail["rating"]["comment"] == "Fresh"
    again = await api.http.post(f"/orders/{order_id}/rating", json={"score": 4})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ALREADY_RATED"
    # catalog keeps the farm's average from this one event (and a refused repeat sends nothing)
    (event,) = await api.outbox("order.rated")
    assert event.payload["orderId"] == order_id
    assert event.payload["score"] == 5
    assert event.payload["farmId"] and event.payload["customerId"]


async def test_cannot_rate_before_completion(api):
    order = await api.placed_order()

    response = await api.http.post(f"/orders/{order['id']}/rating", json={"score": 5})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_NOT_COMPLETED"


async def test_cannot_rate_someone_elses_order_or_with_a_bad_score(api):
    order_id = await _dispatchable(api)
    await deliver(api, "delivery.completed", orderId=order_id)

    assert (await api.http.post(f"/orders/{order_id}/rating", json={"score": 6})).status_code == 422
    api.login(user(OTHER_CUSTOMER, Role.CUSTOMER))
    assert (await api.http.post(f"/orders/{order_id}/rating", json={"score": 5})).status_code == 404


# --- who sees what -----------------------------------------------------------------------------


async def test_customer_sees_only_own_orders(api):
    await api.placed_order()
    api.login(user(OTHER_CUSTOMER, Role.CUSTOMER))

    assert (await api.http.get("/orders")).json()["items"] == []


async def test_order_of_another_customer_looks_missing(api):
    order = await api.placed_order()
    api.login(user(OTHER_CUSTOMER, Role.CUSTOMER))

    response = await api.http.get(f"/orders/{order['id']}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ORDER_NOT_FOUND"


async def test_farmer_sees_only_orders_of_own_farm(api):
    await api.placed_order()
    api.login(FARMER)

    items = (await api.http.get("/orders")).json()["items"]

    assert [i["farmId"] for i in items] == [str(FARM_A)]
    api.login(user(FARMER_B, Role.FARMER))
    assert (await api.http.get("/orders")).json()["items"][0]["farmId"] == str(FARM_B)


async def test_role_filter_chooses_between_buying_and_selling(api):
    await api.placed_order()
    both = user(FARMER_A, Role.CUSTOMER, Role.FARMER)
    api.login(both)

    selling = (await api.http.get("/orders", params={"role": "farmer"})).json()["items"]
    buying = (await api.http.get("/orders", params={"role": "customer"})).json()["items"]

    assert len(selling) == 1
    assert buying == []


async def test_admin_sees_everything(api):
    await api.placed_order()
    api.login(ADMIN)

    assert len((await api.http.get("/orders")).json()["items"]) == 2


async def test_list_filter_and_cursor_pagination(api):
    first = await api.placed_order(FARM_A)
    await api.http.post(f"/orders/{first['id']}/cancel")
    for key in ("second-key-1", "third-key-01"):
        assert (await api.checkout(key=key)).status_code == 201  # 4 more orders: 6 in total

    cancelled = (await api.http.get("/orders", params={"status": "CANCELLED"})).json()
    assert [o["id"] for o in cancelled["items"]] == [first["id"]]
    assert cancelled["nextCursor"] is None

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 4} | ({"cursor": cursor} if cursor else {})
        page = (await api.http.get("/orders", params=params)).json()
        seen += [o["id"] for o in page["items"]]
        pages += 1
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert pages == 2
    assert len(seen) == len(set(seen)) == 6

    assert (await api.http.get("/orders", params={"status": "SHIPPED"})).status_code == 422
    assert (await api.http.get("/orders", params={"limit": 0})).status_code == 422
    bad = await api.http.get("/orders", params={"cursor": "garbage"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_CURSOR"


async def test_get_order_rejects_bad_id(api):
    assert (await api.http.get("/orders/not-a-uuid")).status_code == 422
    assert (await api.http.get(f"/orders/{uuid4()}")).status_code == 404


async def test_order_items_keep_the_price_from_checkout_time(api):
    order = await api.placed_order(FARM_B)

    api.stock.listings[SPINACH][4] = api.stock.listings[SPINACH][4] * 2  # price changes later
    detail = (await api.http.get(f"/orders/{order['id']}")).json()

    assert detail["items"][0]["unitPrice"] == 25.5
    assert detail["items"][0]["name"] == "Spinach"


async def test_cancelling_records_a_refund_payment(api):
    from sqlalchemy import select

    from order_service.db import PaymentRow

    order = await api.placed_order()

    await api.http.post(f"/orders/{order['id']}/cancel")

    async with api.session_factory() as session:
        refunds = (
            (await session.execute(select(PaymentRow).where(PaymentRow.kind == "REFUND")))
            .scalars()
            .all()
        )
    assert [(str(r.order_id), float(r.amount), r.status) for r in refunds] == [
        (order["id"], order["total"], "SUCCEEDED")
    ]


async def test_the_farmer_can_cancel_an_order_of_their_farm(api):
    order = await api.placed_order(FARM_A)
    api.login(FARMER)

    response = await api.http.post(f"/orders/{order['id']}/cancel", json={"reason": "Sold out"})

    assert response.status_code == 200
    (event,) = await api.outbox("order.cancelled")
    assert event.payload["cancelledBy"] == "farmer"
    assert event.payload["reason"] == "Sold out"


async def test_another_farmer_cannot_cancel_it(api):
    order = await api.placed_order(FARM_A)
    api.login(user(FARMER_B, Role.FARMER))

    assert (await api.http.post(f"/orders/{order['id']}/cancel")).status_code == 404


# --- the feed logistics pools from -------------------------------------------------------------


async def ready_order(api, farm=FARM_A) -> dict:
    order = await api.placed_order(farm)
    await pack(api, order["id"])
    return order


async def test_dispatch_feed_lists_ready_orders_in_the_shape_logistics_reads(api):
    order = await ready_order(api)
    # the other farm's order of the same checkout is still CONFIRMED: not listed

    response = await api.http.get(
        "/internal/orders/ready-for-dispatch", params={"date": "2999-01-01"}
    )

    assert response.status_code == 200
    (row,) = response.json()
    assert row == {
        "order_id": order["id"],
        "delivery_mode": "IMMEDIATE",
        "delivery_date": order["deliveryDate"],
        "hub_lat": 26.85,
        "hub_lng": 75.8,
        "drop_lat": 26.9124,
        "drop_lng": 75.7873,
        "weight_kg": 2.0,
        "address": "12 Green Park, Jaipur, 302001",
        "customer_id": str(CUSTOMER),
        "contact_phone": None,
    }


async def test_dispatch_feed_respects_the_delivery_date_and_drops_pooled_orders(api):
    order = await ready_order(api)  # delivery date: tomorrow by default

    too_early = await api.http.get(
        "/internal/orders/ready-for-dispatch", params={"date": "2000-01-01"}
    )
    assert too_early.json() == []

    await deliver(api, "pool.assigned", orderIds=[order["id"]])
    pooled = await api.http.get(
        "/internal/orders/ready-for-dispatch", params={"date": "2999-01-01"}
    )
    assert pooled.json() == []


async def test_completing_an_order_records_the_farmers_payout(api):
    order_id = await _dispatchable(api)
    await deliver(api, "delivery.completed", orderId=order_id)

    detail = (await api.http.get(f"/orders/{order_id}")).json()
    payout = [p for p in detail["payments"] if p["kind"] == "PAYOUT"]
    assert len(payout) == 1 and payout[0]["amount"] == detail["subtotal"]

    api.login(FARMER)
    mine = (await api.http.get("/orders/payouts")).json()
    assert [p["orderId"] for p in mine["items"]] == [order_id]
    assert mine["total"] == detail["subtotal"]
    api.login(user(uuid4(), Role.FARMER))
    assert (await api.http.get("/orders/payouts")).json() == {"items": [], "total": 0}


async def test_the_order_list_shows_the_customers_rating(api):
    order_id = await _dispatchable(api)
    await deliver(api, "delivery.completed", orderId=order_id)

    before = (await api.http.get("/orders")).json()["items"]
    await api.http.post(f"/orders/{order_id}/rating", json={"score": 4})
    after = (await api.http.get("/orders")).json()["items"]

    assert [o["ratingScore"] for o in before if o["id"] == order_id] == [None]
    assert [o["ratingScore"] for o in after if o["id"] == order_id] == [4]
