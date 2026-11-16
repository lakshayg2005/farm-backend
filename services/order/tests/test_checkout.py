from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from farm_common.auth import Role, get_current_user
from order_service.events import OrderCancelledPayload, OrderEventPayload
from order_service.main import app

from .conftest import (
    CUSTOMER,
    FAIL_AMOUNT,
    FARM_A,
    FARM_B,
    ONION,
    OTHER_CUSTOMER,
    SPINACH,
    TOMATO,
    user,
)


async def test_checkout_creates_one_confirmed_order_per_farm(api):
    response = await api.checkout()

    assert response.status_code == 201
    body = response.json()
    assert {o["farmId"] for o in body["orders"]} == {str(FARM_A), str(FARM_B)}
    assert {o["status"] for o in body["orders"]} == {"CONFIRMED"}
    # tomato 2 x 40 + 20 fee, spinach 1 x 25.50 + 20 fee
    assert sorted(o["total"] for o in body["orders"]) == [45.5, 100.0]
    assert body["subtotal"] == 105.5
    assert body["deliveryFee"] == 40.0
    assert body["total"] == 145.5
    assert body["paymentStatus"] == "SUCCEEDED"
    assert api.stock.available(TOMATO) == 98


async def test_checkout_publishes_order_placed_for_each_order(api):
    body = (await api.checkout()).json()

    events = await api.outbox("order.placed")

    assert len(events) == 2
    order_ids = {e.payload["orderId"] for e in events}
    assert order_ids == {o["id"] for o in body["orders"]}
    for event in events:
        payload = OrderEventPayload.model_validate(event.payload)
        assert str(payload.customer_id) == body["customerId"]
        assert payload.status == "CONFIRMED"
        assert payload.delivery_address.location.lat == 26.9124
        assert payload.items[0].pickup_location.lng == 75.8
        assert payload.delivery_date
        assert payload.items


async def test_order_timeline_starts_with_placed_then_confirmed(api):
    order = await api.placed_order()

    detail = (await api.http.get(f"/orders/{order['id']}")).json()

    assert [e["status"] for e in detail["events"]] == ["PLACED", "CONFIRMED"]


async def test_a_used_payment_intent_cannot_be_used_again(api):
    intent_id = await api.intent(amount=1)
    await api.checkout(payment_intent_id=intent_id)

    response = await api.checkout(payment_intent_id=intent_id, key="checkout-0002")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PAYMENT_INTENT_USED"


async def test_same_listing_twice_is_one_line(api):
    items = [
        {"listingId": str(TOMATO), "qty": 1},
        {"listingId": str(TOMATO), "qty": 2.5},
    ]

    response = await api.checkout(items=items)

    order = response.json()["orders"][0]
    assert len(order["items"]) == 1
    assert order["items"][0]["qty"] == 3.5
    assert order["subtotal"] == 140.0


async def test_repeating_a_checkout_returns_the_first_result(api):
    intent_id = await api.intent()
    first = await api.checkout(key="same-key-01", payment_intent_id=intent_id)

    again = await api.checkout(key="same-key-01", payment_intent_id=intent_id)

    assert first.status_code == 201
    assert again.status_code == 201
    assert again.json() == first.json()
    assert len(api.stock.reserved) == 2  # one reservation per farm order, made only once
    assert len(await api.outbox("order.placed")) == 2
    assert len((await api.http.get("/orders")).json()["items"]) == 2


async def test_same_key_with_a_different_request_is_rejected(api):
    await api.checkout(key="same-key-01")

    response = await api.checkout(key="same-key-01", items=[{"listingId": str(ONION), "qty": 1}])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_out_of_stock_fails_cleanly_and_the_key_can_be_reused(api):
    items = [{"listingId": str(TOMATO), "qty": 500}]
    intent_id = await api.intent()

    response = await api.checkout(items=items, payment_intent_id=intent_id, key="retry-key-1")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "STOCK_UNAVAILABLE"
    assert error["details"] == [{"listingId": str(TOMATO), "requested": 500.0, "available": 100.0}]
    assert await api.outbox() == []
    assert (await api.http.get("/orders")).json()["items"] == []

    api.stock.listings[TOMATO][5] = Decimal(600)  # stock comes back
    retry = await api.checkout(items=items, payment_intent_id=intent_id, key="retry-key-1")

    assert retry.status_code == 201


async def test_declined_payment_cancels_the_orders_and_publishes_cancelled(api):
    response = await api.checkout(amount=float(FAIL_AMOUNT))

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "PAYMENT_FAILED"
    orders = (await api.http.get("/orders")).json()["items"]
    assert [o["status"] for o in orders] == ["CANCELLED", "CANCELLED"]
    assert await api.outbox("order.placed") == []
    cancelled = await api.outbox("order.cancelled")
    assert len(cancelled) == 2  # catalog releases the stock when it receives these
    payload = OrderCancelledPayload.model_validate(cancelled[0].payload)
    assert payload.reason == "PAYMENT_FAILED"
    assert payload.cancelled_by == "system"
    assert sum(len(e.payload["items"]) for e in cancelled) == 2


async def test_declined_payment_result_is_replayed_for_the_same_key(api):
    intent_id = await api.intent(amount=float(FAIL_AMOUNT))
    first = await api.checkout(key="declined-01", payment_intent_id=intent_id)

    again = await api.checkout(key="declined-01", payment_intent_id=intent_id)

    assert first.status_code == 402
    assert again.status_code == 402
    assert again.json() == first.json()
    assert len(api.stock.reserved) == 2


async def test_cannot_use_someone_elses_payment_intent(api):
    api.login(user(OTHER_CUSTOMER, Role.CUSTOMER))
    intent_id = await api.intent()
    api.login(user(CUSTOMER, Role.CUSTOMER))

    response = await api.checkout(payment_intent_id=intent_id)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PAYMENT_INTENT_NOT_FOUND"


async def test_unknown_payment_intent(api):
    response = await api.checkout(payment_intent_id=str(uuid4()))

    assert response.status_code == 404


async def test_checkout_requires_idempotency_key(api):
    body = {"items": [{"listingId": str(TOMATO), "qty": 1}]}

    response = await api.http.post("/orders", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "bad_items",
    [[], [{"listingId": str(TOMATO), "qty": 0}], [{"listingId": "nope", "qty": 1}]],
)
async def test_checkout_rejects_invalid_items(api, bad_items):
    response = await api.http.post(
        "/orders",
        json={
            "items": bad_items,
            "deliveryAddress": {
                "line1": "x",
                "city": "y",
                "pincode": "302001",
                "location": {"type": "Point", "coordinates": [1, 1]},
            },
            "paymentIntentId": str(uuid4()),
        },
        headers={"Idempotency-Key": "validation-1"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "address_change",
    [
        {"pincode": "12"},
        {"location": {"type": "Point", "coordinates": [1, 123]}},
        {"location": {"type": "Point", "coordinates": [-200, 1]}},
        {"location": None},
        {"city": None},
    ],
)
async def test_checkout_rejects_invalid_address(api, address_change):
    address = {
        "line1": "x",
        "city": "y",
        "pincode": "302001",
        "location": {"type": "Point", "coordinates": [1, 1]},
        **address_change,
    }

    response = await api.http.post(
        "/orders",
        json={
            "items": [{"listingId": str(TOMATO), "qty": 1}],
            "deliveryAddress": address,
            "paymentIntentId": str(uuid4()),
        },
        headers={"Idempotency-Key": "validation-2"},
    )

    assert response.status_code == 422


async def test_payment_intent_rejects_non_positive_amount(api):
    assert (await api.http.post("/orders/payments/intent", json={"amount": 0})).status_code == 422


async def test_requires_login(api):
    del app.dependency_overrides[get_current_user]

    response = await api.http.get("/orders")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_delivery_partner_cannot_checkout(api):
    api.login(user(OTHER_CUSTOMER, Role.DELIVERY))

    assert (await api.http.post("/orders/payments/intent", json={"amount": 5})).status_code == 403


async def test_each_order_is_reserved_in_catalog_under_its_own_id(api):
    body = (await api.checkout()).json()

    order_ids = {UUID(o["id"]) for o in body["orders"]}

    assert set(api.stock.reserved) == order_ids
    farm_a = next(o for o in body["orders"] if o["farmId"] == str(FARM_A))
    assert [i.listing_id for i in api.stock.reserved[UUID(farm_a["id"])]] == [TOMATO]


async def test_stock_for_a_farm_that_ran_out_releases_the_other_farms_reservation(api):
    api.stock.listings[SPINACH][5] = Decimal(0)  # farm B (reserved second) is sold out
    items = [{"listingId": str(TOMATO), "qty": 2}, {"listingId": str(SPINACH), "qty": 1}]

    response = await api.checkout(items=items)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STOCK_UNAVAILABLE"
    assert api.stock.available(TOMATO) == 100  # farm A's stock was given back
    assert api.stock.reserved == {}
    assert await api.outbox() == []
    assert (await api.http.get("/orders")).json()["items"] == []


async def test_unknown_listing_is_a_404(api):
    response = await api.checkout(items=[{"listingId": str(uuid4()), "qty": 1}])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "LISTING_NOT_FOUND"
    assert api.stock.reserved == {}


async def test_orders_get_a_delivery_date_tomorrow_unless_chosen(api):
    from datetime import timedelta

    from order_service.service import _today

    default = (await api.checkout()).json()["orders"][0]
    chosen_day = (_today() + timedelta(days=3)).isoformat()
    chosen = (await api.checkout(key="chosen-day-1", deliveryDate=chosen_day)).json()["orders"][0]

    assert default["deliveryDate"] == (_today() + timedelta(days=1)).isoformat()
    assert chosen["deliveryDate"] == chosen_day


@pytest.mark.parametrize("days", [-1, 31])
async def test_delivery_date_must_be_within_the_next_30_days(api, days):
    from datetime import timedelta

    from order_service.service import _today

    response = await api.checkout(deliveryDate=(_today() + timedelta(days=days)).isoformat())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_DELIVERY_DATE"
    assert api.stock.reserved == {}  # rejected before any stock was reserved


async def test_orders_carry_the_farms_pickup_point(api):
    order = await api.placed_order()

    detail = (await api.http.get(f"/orders/{order['id']}")).json()

    assert detail["items"][0]["pickupLocation"] == {"type": "Point", "coordinates": [75.8, 26.85]}
    assert detail["deliveryAddress"]["location"]["coordinates"] == [75.7873, 26.9124]


async def test_a_successful_checkout_publishes_payment_succeeded(api):
    body = (await api.checkout()).json()

    (event,) = await api.outbox("payment.succeeded")

    assert event.payload["kind"] == "FULL"
    assert event.payload["amount"] == body["total"]
    assert event.payload["checkoutId"] == body["id"]
    assert set(event.payload["orderIds"]) == {o["id"] for o in body["orders"]}


async def test_a_declined_payment_publishes_no_payment_succeeded(api):
    await api.checkout(amount=float(FAIL_AMOUNT))

    assert await api.outbox("payment.succeeded") == []


async def test_only_full_payments_can_pay_for_an_order(api):
    deposit = await api.http.post(
        "/orders/payments/intent",
        json={"amount": 500, "kind": "DEPOSIT", "referenceId": str(uuid4())},
    )

    response = await api.checkout(payment_intent_id=deposit.json()["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PAYMENT_INTENT_USED"
