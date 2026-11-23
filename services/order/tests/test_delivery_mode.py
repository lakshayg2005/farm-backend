"""IMMEDIATE vs FLEXIBLE delivery: the fee, the event logistics reads, the batch feed, the time."""

from datetime import datetime
from decimal import Decimal

from order_service import delivery
from order_service.settings import OrderSettings

from .test_lifecycle import deliver, pack

FEED = "/internal/orders/ready-for-dispatch"


async def flexible_order(api) -> dict:
    response = await api.checkout(deliveryMode="FLEXIBLE")
    assert response.status_code == 201, response.text
    return response.json()["orders"][0]


async def test_checkout_defaults_to_immediate_with_the_normal_fee(api):
    response = await api.checkout()
    order = response.json()["orders"][0]
    assert order["deliveryMode"] == "IMMEDIATE"
    assert order["deliveryFee"] == 20.0
    assert order["batchAt"] is None


async def test_flexible_checkout_has_the_lower_fee_and_a_batch_time(api):
    response = await api.checkout(deliveryMode="FLEXIBLE")
    body = response.json()
    assert {o["deliveryMode"] for o in body["orders"]} == {"FLEXIBLE"}
    assert {o["deliveryFee"] for o in body["orders"]} == {10.0}
    assert body["deliveryFee"] == 10.0 * len(body["orders"])
    assert all(o["batchAt"] for o in body["orders"])

    detail = (await api.http.get(f"/orders/{body['orders'][0]['id']}")).json()
    assert detail["deliveryMode"] == "FLEXIBLE" and detail["batchAt"]
    listed = (await api.http.get("/orders")).json()["items"]
    assert {i["deliveryMode"] for i in listed} == {"FLEXIBLE"}


async def test_unknown_delivery_mode_is_rejected(api):
    assert (await api.checkout(deliveryMode="TOMORROW-ISH")).status_code == 422


async def test_events_carry_the_mode_so_logistics_can_hold_flexible_orders(api):
    order = await flexible_order(api)
    await pack(api, order["id"])
    (ready,) = await api.outbox("order.ready_for_dispatch")
    assert ready.payload["deliveryMode"] == "FLEXIBLE"


async def test_packing_a_flexible_order_says_it_waits_for_the_batch(api):
    order = await flexible_order(api)
    await pack(api, order["id"])
    detail = (await api.http.get(f"/orders/{order['id']}")).json()
    assert detail["status"] == "READY_FOR_DISPATCH"
    assert "shared-delivery batch" in detail["events"][-1]["note"]
    assert detail["batchAt"]  # still waiting


async def test_dispatch_feed_can_be_filtered_by_mode(api):
    flexible = await flexible_order(api)
    await pack(api, flexible["id"])
    immediate = (await api.checkout(key="checkout-0002")).json()["orders"][0]
    await pack(api, immediate["id"])
    params = {"date": "2999-01-01"}

    both = (await api.http.get(FEED, params=params)).json()
    only_flex = (await api.http.get(FEED, params={**params, "mode": "FLEXIBLE"})).json()
    only_now = (await api.http.get(FEED, params={**params, "mode": "IMMEDIATE"})).json()

    assert {r["order_id"] for r in both} == {flexible["id"], immediate["id"]}
    assert [r["order_id"] for r in only_flex] == [flexible["id"]]
    assert [r["order_id"] for r in only_now] == [immediate["id"]]


async def test_the_batch_time_is_not_reported_once_the_order_is_on_a_route(api):
    order = await flexible_order(api)
    await pack(api, order["id"])
    await deliver(api, "pool.assigned", orderIds=[order["id"]])
    detail = (await api.http.get(f"/orders/{order['id']}")).json()
    assert detail["status"] == "POOLED" and detail["batchAt"] is None


async def test_delivery_options_lists_both_modes_and_the_batch_time(api):
    response = await api.http.get("/orders/delivery-options")
    assert response.status_code == 200
    body = response.json()
    assert [(o["mode"], o["feePerOrder"]) for o in body["options"]] == [
        ("IMMEDIATE", 20.0),
        ("FLEXIBLE", 10.0),
    ]
    assert body["batchTime"] == "10:00"
    assert datetime.fromisoformat(body["nextBatchAt"]) > datetime.now().astimezone()


# --- the schedule ------------------------------------------------------------------------------

S = OrderSettings()
IST = delivery.IST


def at(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=IST)


def test_before_ten_the_batch_is_today_at_ten():
    assert delivery.next_batch_at(S, None, at(2026, 11, 24, 8)) == at(2026, 11, 24, 10)


def test_after_ten_the_batch_is_tomorrow():
    assert delivery.next_batch_at(S, None, at(2026, 11, 24, 10, 1)) == at(2026, 11, 25, 10)


def test_an_order_due_later_waits_for_its_own_day():
    due = at(2026, 11, 27, 0).date()
    assert delivery.next_batch_at(S, due, at(2026, 11, 24, 8)) == at(2026, 11, 27, 10)


def test_a_past_delivery_date_does_not_move_the_batch_back():
    due = at(2026, 11, 1, 0).date()
    assert delivery.next_batch_at(S, due, at(2026, 11, 24, 8)) == at(2026, 11, 24, 10)


def test_fee_of_each_mode():
    assert delivery.fee_for("FLEXIBLE", S) == Decimal("10.00")
    assert delivery.fee_for("IMMEDIATE", S) == Decimal("20.00")
