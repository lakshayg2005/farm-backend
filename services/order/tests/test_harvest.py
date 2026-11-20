"""Harvest orders (issue #24): deposits, the order created on a quality pass, DELIVERY or BUYBACK,
balance on completion, refunds."""

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from farm_common.auth import Role
from farm_common.events import Event
from order_service import consumers
from order_service.db import PaymentRow
from order_service.events import OrderEventPayload, PaymentSucceededPayload
from order_service.main import app
from order_service.payments import MockPaymentGateway
from order_service.settings import OrderSettings, get_order_settings

from .conftest import CUSTOMER, FAIL_AMOUNT, FARMER_A, OTHER_CUSTOMER, user

RESERVATION = UUID("aaaaaaaa-0000-4000-8000-000000000001")
PLOT = {"type": "Point", "coordinates": [75.9, 26.7]}
SETTINGS = OrderSettings(
    delivery_fee_per_order=Decimal("20.00"), mock_payment_fail_amount=FAIL_AMOUNT
)
ADDRESS = {
    "line1": "12 Green Park",
    "city": "Jaipur",
    "pincode": "302001",
    "location": {"type": "Point", "coordinates": [75.7873, 26.9124]},
}


async def deliver(api, event_type: str, gateway=None, **payload) -> Event:
    event = Event(type=event_type, producer="test", payload=payload)
    await consumers.handle_event(api.session_factory, event, SETTINGS, gateway)
    return event


async def accept(api, deposit=200, total=1000, expected=100, reservation=RESERVATION) -> None:
    """land accepted a reservation of 100 kg tomatoes for 1000 rupees, deposit 200."""
    payload = {
        "reservationId": str(reservation),
        "customerId": str(CUSTOMER),
        "farmerId": str(FARMER_A),
        "deposit": deposit,
    }
    if total is not None:
        payload |= {"total": total, "cropId": "tomato", "expectedQtyKg": expected}
    await deliver(api, "reservation.accepted", **payload)


async def pay_deposit(api, amount=200, reservation=RESERVATION):
    return await api.http.post(
        "/orders/payments/intent",
        json={"amount": amount, "kind": "DEPOSIT", "referenceId": str(reservation)},
    )


async def harvest_ready(api, reservation=RESERVATION) -> None:
    await deliver(
        api,
        "harvest.ready",
        reservationId=str(reservation),
        farmerId=str(FARMER_A),
        customerId=str(CUSTOMER),
        cropId="tomato",
        expectedQtyKg=100,
        plotCentroid=PLOT,
    )


async def graded(
    api, result="PASS", grade="A", weight=90, resolution=None, reservation=RESERVATION
):
    return await deliver(
        api,
        "quality.graded",
        inspectionId=str(uuid4()),
        subjectType="HARVEST",
        subjectId=str(reservation),
        grade=grade,
        result=result,
        resolution=resolution,
        weightKg=weight,
    )


async def graded_harvest_order(api, **graded_kwargs) -> dict:
    """Accepted, deposit paid, harvest ready and graded: returns the harvest order."""
    await accept(api)
    assert (await pay_deposit(api)).json()["status"] == "SUCCEEDED"
    await harvest_ready(api)
    await graded(api, **graded_kwargs)
    return await only_order(api)


async def only_order(api) -> dict:
    listed = (await api.http.get("/orders")).json()["items"]
    assert len(listed) == 1, listed
    return (await api.http.get(f"/orders/{listed[0]['id']}")).json()


async def choose(api, option, **extra):
    body = {"reservationId": str(RESERVATION), "option": option, **extra}
    return await api.http.post("/orders/harvest", json=body)


# --- deposits ----------------------------------------------------------------------------------


async def test_deposit_needs_an_accepted_reservation(api):
    response = await pay_deposit(api)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESERVATION_NOT_FOUND"


async def test_deposit_is_taken_at_once_and_land_is_told(api):
    await accept(api)

    response = await pay_deposit(api)

    assert response.status_code == 201
    assert response.json()["status"] == "SUCCEEDED"
    assert response.json()["kind"] == "DEPOSIT"
    (event,) = await api.outbox("payment.succeeded")
    payload = PaymentSucceededPayload.model_validate(event.payload)
    assert payload.kind == "DEPOSIT"
    assert payload.reference_id == RESERVATION
    assert payload.amount == 200
    assert payload.customer_id == CUSTOMER


async def test_deposit_must_match_the_reservation(api):
    await accept(api)

    wrong_amount = await pay_deposit(api, amount=150)
    other_customer = await _as(api, OTHER_CUSTOMER, pay_deposit)

    assert wrong_amount.status_code == 422
    assert wrong_amount.json()["error"]["code"] == "DEPOSIT_AMOUNT_MISMATCH"
    assert other_customer.status_code == 404  # someone else's reservation looks like a missing one
    assert await api.outbox("payment.succeeded") == []


async def _as(api, who, call):
    api.login(user(who, Role.CUSTOMER))
    try:
        return await call(api)
    finally:
        api.login(user(CUSTOMER, Role.CUSTOMER))


async def test_deposit_is_paid_only_once(api):
    await accept(api)
    await pay_deposit(api)

    again = await pay_deposit(api)

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "DEPOSIT_ALREADY_PAID"
    assert len(await api.outbox("payment.succeeded")) == 1


async def test_a_declined_deposit_is_failed_and_publishes_nothing(api):
    await accept(api, deposit=float(FAIL_AMOUNT))

    response = await pay_deposit(api, amount=float(FAIL_AMOUNT))

    assert response.status_code == 201
    assert response.json()["status"] == "FAILED"
    assert await api.outbox("payment.succeeded") == []


async def test_clients_cannot_create_balance_or_refund_payments_or_a_deposit_without_reservation(
    api,
):
    for body in (
        {"amount": 10, "kind": "BALANCE", "referenceId": str(RESERVATION)},
        {"amount": 10, "kind": "REFUND"},
        {"amount": 10, "kind": "DEPOSIT"},
    ):
        response = await api.http.post("/orders/payments/intent", json=body)
        assert response.status_code == 422, body


async def test_only_full_payments_can_pay_for_an_order(api):
    await accept(api)
    deposit = await pay_deposit(api)

    response = await api.checkout(payment_intent_id=deposit.json()["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PAYMENT_INTENT_USED"


# --- webhook -----------------------------------------------------------------------------------


async def pending_deposit(api) -> str:
    app.dependency_overrides[get_order_settings] = lambda: SETTINGS.model_copy(
        update={"mock_auto_settle": False}
    )
    await accept(api)
    response = await pay_deposit(api)
    assert response.json()["status"] == "PENDING"
    return response.json()["id"]


def hook(api, intent_id: str, status: str, secret: str = "dev-webhook-secret"):
    return api.http.post(
        "/orders/payments/webhook",
        json={"paymentIntentId": intent_id, "status": status},
        headers={"X-Webhook-Secret": secret},
    )


async def test_webhook_settles_a_pending_deposit_once(api):
    intent_id = await pending_deposit(api)
    api.login(user(uuid4(), Role.ADMIN))  # the gateway is not a user: the secret is what counts

    assert (await hook(api, intent_id, "SUCCEEDED", secret="wrong")).status_code == 401
    first = await hook(api, intent_id, "SUCCEEDED")
    repeat = await hook(api, intent_id, "SUCCEEDED")
    contradiction = await hook(api, intent_id, "FAILED")

    assert first.status_code == 200
    assert first.json()["status"] == "SUCCEEDED"
    assert repeat.status_code == 200
    assert contradiction.status_code == 409
    assert contradiction.json()["error"]["code"] == "PAYMENT_ALREADY_SETTLED"
    assert len(await api.outbox("payment.succeeded")) == 1


async def test_webhook_failure_and_unknown_or_full_intents(api):
    intent_id = await pending_deposit(api)
    full = await api.intent(100)

    failed = await hook(api, intent_id, "FAILED")
    unknown = await hook(api, str(uuid4()), "SUCCEEDED")
    not_a_deposit = await hook(api, full, "SUCCEEDED")

    assert failed.json()["status"] == "FAILED"
    assert unknown.status_code == 404
    assert not_a_deposit.status_code == 409
    assert not_a_deposit.json()["error"]["code"] == "PAYMENT_NOT_SETTLEABLE"
    assert await api.outbox("payment.succeeded") == []


# --- the harvest order -------------------------------------------------------------------------


async def test_quality_pass_creates_the_harvest_order(api):
    order = await graded_harvest_order(api)

    assert order["orderType"] == "HARVEST"
    assert order["status"] == "PLACED"
    assert order["reservationId"] == str(RESERVATION)
    assert order["farmId"] is None
    assert order["harvestOption"] is None
    assert order["deliveryAddress"] is None
    (item,) = order["items"]
    assert item["name"] == "Harvest: Tomato"
    assert item["qty"] == 90  # the graded weight
    assert item["lineTotal"] == 900  # 10 rupees a kg, as agreed in the reservation
    assert item["listingId"] is None
    assert item["pickupLocation"] == PLOT
    assert order["total"] == 900
    assert order["balanceDue"] == 700  # minus the 200 deposit
    assert [p["kind"] for p in order["payments"]] == ["DEPOSIT"]
    (placed,) = await api.outbox("order.placed")
    payload = OrderEventPayload.model_validate(placed.payload)
    assert payload.order_type == "HARVEST"
    assert payload.reservation_id == RESERVATION
    assert payload.farm_owner_id == FARMER_A
    assert payload.delivery_address is None


async def test_the_farmer_sees_the_harvest_order(api):
    await graded_harvest_order(api)

    api.login(user(FARMER_A, Role.FARMER))
    listed = (await api.http.get("/orders", params={"role": "farmer"})).json()["items"]

    assert [o["orderType"] for o in listed] == ["HARVEST"]


async def test_the_same_quality_result_twice_makes_one_order(api):
    await accept(api)
    await pay_deposit(api)
    await harvest_ready(api)
    event = await graded(api)
    await consumers.handle_event(api.session_factory, event, SETTINGS)
    await graded(api)  # a different event for the same harvest

    assert len((await api.http.get("/orders")).json()["items"]) == 1
    assert len(await api.outbox("order.placed")) == 1


async def test_a_failed_harvest_makes_no_order_until_a_lower_grade_is_accepted(api):
    await accept(api)
    await pay_deposit(api)
    await harvest_ready(api)

    await graded(api, result="FAIL", grade="C", resolution="REHARVEST")
    assert (await api.http.get("/orders")).json()["items"] == []

    await graded(api, result="FAIL", grade="B", resolution="ACCEPT_LOWER_GRADE")
    order = await only_order(api)
    assert order["total"] == 810  # grade B sells for 90% of 900
    assert "lower grade" in order["events"][0]["note"]


async def test_without_a_price_in_the_reservation_it_is_estimated_from_the_deposit(api):
    await accept(api, total=None, deposit=200)  # 200 is 20% of the crop, so the crop is 1000
    await pay_deposit(api)
    await harvest_ready(api)
    await graded(api, weight=100)

    assert (await only_order(api))["total"] == 1000


async def test_events_in_the_wrong_order_are_tolerated(api):
    await harvest_ready(api)  # before reservation.accepted
    await accept(api)
    await pay_deposit(api)
    await graded(api)

    assert (await only_order(api))["items"][0]["pickupLocation"] == PLOT


async def test_a_graded_harvest_of_an_unknown_reservation_is_ignored(api):
    await graded(api)

    assert (await api.http.get("/orders")).json()["items"] == []


# --- choosing delivery -------------------------------------------------------------------------


async def test_delivery_sends_the_order_to_logistics(api):
    await graded_harvest_order(api)
    tomorrow = (date.today() + timedelta(days=2)).isoformat()

    response = await choose(api, "DELIVERY", deliveryAddress=ADDRESS, deliveryDate=tomorrow)

    assert response.status_code == 200, response.text
    order = response.json()
    assert order["status"] == "READY_FOR_DISPATCH"
    assert order["harvestOption"] == "DELIVERY"
    assert order["deliveryFee"] == 20
    assert order["total"] == 920
    assert order["balanceDue"] == 720
    assert order["deliveryAddress"]["city"] == "Jaipur"
    assert [e["status"] for e in order["events"]] == [
        "PLACED",
        "CONFIRMED",
        "PACKED",
        "QC_PASSED",
        "READY_FOR_DISPATCH",
    ]
    (ready,) = await api.outbox("order.ready_for_dispatch")
    payload = OrderEventPayload.model_validate(ready.payload)
    assert payload.delivery_address is not None
    assert payload.items[0].pickup_location.lng == 75.9
    assert payload.delivery_date.isoformat() == tomorrow
    # quality must not open a second inspection for a harvest that is already graded
    assert await api.outbox("order.packed") == []


async def test_delivery_needs_an_address_and_repeating_the_choice_is_safe(api):
    await graded_harvest_order(api)

    missing = await choose(api, "DELIVERY")
    first = await choose(api, "DELIVERY", deliveryAddress=ADDRESS)
    repeat = await choose(api, "DELIVERY", deliveryAddress=ADDRESS)
    other = await choose(api, "BUYBACK")

    assert missing.status_code == 422
    assert first.status_code == repeat.status_code == 200
    assert len(await api.outbox("order.ready_for_dispatch")) == 1
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "HARVEST_OPTION_CHOSEN"


async def test_delivery_date_defaults_to_tomorrow_and_must_be_valid(api):
    await graded_harvest_order(api)
    past = (date.today() - timedelta(days=3)).isoformat()

    bad = await choose(api, "DELIVERY", deliveryAddress=ADDRESS, deliveryDate=past)
    good = await choose(api, "DELIVERY", deliveryAddress=ADDRESS)

    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "INVALID_DELIVERY_DATE"
    from datetime import datetime

    tomorrow = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
    assert good.json()["deliveryDate"] == tomorrow.isoformat()


async def test_choice_needs_a_graded_harvest_of_my_own_reservation(api):
    await accept(api)

    too_early = await choose(api, "BUYBACK")
    assert too_early.status_code == 409
    assert too_early.json()["error"]["code"] == "HARVEST_NOT_READY"

    await pay_deposit(api)
    await harvest_ready(api)
    await graded(api)
    stranger = await _as(api, OTHER_CUSTOMER, lambda a: choose(a, "BUYBACK"))
    assert stranger.status_code == 404
    unknown = await api.http.post(
        "/orders/harvest", json={"reservationId": str(uuid4()), "option": "BUYBACK"}
    )
    assert unknown.status_code == 404


# --- balance on completion ---------------------------------------------------------------------


async def delivered(api, gateway=None) -> dict:
    order = await graded_harvest_order(api)
    await choose(api, "DELIVERY", deliveryAddress=ADDRESS)
    await deliver(api, "delivery.completed", gateway, orderId=order["id"])
    return (await api.http.get(f"/orders/{order['id']}")).json()


async def test_delivery_completed_settles_the_balance_and_completes_the_order(api):
    order = await delivered(api)

    assert order["status"] == "COMPLETED"
    assert order["balanceDue"] == 0
    kinds = [(p["kind"], p["amount"], p["status"]) for p in order["payments"]]
    assert kinds == [
        ("DEPOSIT", 200, "SUCCEEDED"),
        ("BALANCE", 720, "SUCCEEDED"),
        ("PAYOUT", 900, "SUCCEEDED"),  # the farmer is paid the crop value, not the delivery fee
    ]
    balance = [
        PaymentSucceededPayload.model_validate(e.payload)
        for e in await api.outbox("payment.succeeded")
        if e.payload["kind"] == "BALANCE"
    ]
    assert len(balance) == 1
    assert balance[0].reference_id == RESERVATION
    assert [str(i) for i in balance[0].order_ids] == [order["id"]]
    assert len(await api.outbox("order.completed")) == 1
    rating = await api.http.post(f"/orders/{order['id']}/rating", json={"score": 5})
    assert rating.status_code == 201


async def test_a_declined_balance_keeps_the_order_delivered(api):
    order = await delivered(api, gateway=MockPaymentGateway(Decimal("720.00")))

    assert order["status"] == "DELIVERED"
    assert order["balanceDue"] == 720
    assert order["payments"][-1]["kind"] == "BALANCE"
    assert order["payments"][-1]["status"] == "FAILED"
    assert await api.outbox("order.completed") == []


async def test_a_deposit_above_the_lower_grade_price_is_refunded(api):
    order = await graded_harvest_order(api, weight=10)  # 10 kg at 10 rupees = 100 < 200 deposit
    await choose(api, "DELIVERY", deliveryAddress=ADDRESS)  # + 20 delivery = 120
    await deliver(api, "delivery.completed", orderId=order["id"])

    done = (await api.http.get(f"/orders/{order['id']}")).json()

    assert done["status"] == "COMPLETED"
    assert [(p["kind"], p["amount"]) for p in done["payments"]] == [
        ("DEPOSIT", 200),
        ("REFUND", 80),
        ("PAYOUT", 100),
    ]


# --- buy-back ----------------------------------------------------------------------------------


async def test_buyback_credits_the_customer_and_completes_the_order(api):
    await graded_harvest_order(api)

    response = await choose(api, "BUYBACK")

    assert response.status_code == 200, response.text
    order = response.json()
    assert order["status"] == "COMPLETED"
    assert order["harvestOption"] == "BUYBACK"
    assert order["deliveryFee"] == 0
    assert order["balanceDue"] == 0
    assert [(p["kind"], p["amount"], p["status"]) for p in order["payments"]] == [
        ("DEPOSIT", 200, "SUCCEEDED"),
        ("REFUND", 810, "SUCCEEDED"),  # 90% of the 900 harvest
    ]
    assert "credited" in order["events"][-1]["note"]
    assert len(await api.outbox("order.completed")) == 1
    # logistics has nothing to do: no pooling, no inspection
    assert await api.outbox("order.ready_for_dispatch") == []
    assert await api.outbox("order.packed") == []


# --- refunds -----------------------------------------------------------------------------------


async def test_cancelling_a_harvest_order_refunds_the_deposit(api):
    order = await graded_harvest_order(api)

    response = await api.http.post(f"/orders/{order['id']}/cancel", json={"reason": "Changed mind"})

    assert response.status_code == 200, response.text
    cancelled = response.json()
    assert cancelled["status"] == "CANCELLED"
    assert [(p["kind"], p["amount"]) for p in cancelled["payments"]] == [
        ("DEPOSIT", 200),
        ("REFUND", 200),
    ]
    (event,) = await api.outbox("order.cancelled")
    assert event.payload["refundAmount"] == 200
    assert (await choose(api, "BUYBACK")).status_code == 409


async def test_a_harvest_order_can_not_be_cancelled_once_delivery_is_chosen(api):
    order = await graded_harvest_order(api)
    await choose(api, "DELIVERY", deliveryAddress=ADDRESS)

    response = await api.http.post(f"/orders/{order['id']}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ORDER_NOT_CANCELLABLE"


async def test_a_cancelled_reservation_gets_its_deposit_back(api):
    await accept(api)
    await pay_deposit(api)

    await deliver(api, "reservation.cancelled", reservationId=str(RESERVATION))
    await deliver(api, "reservation.cancelled", reservationId=str(RESERVATION))  # a second event
    await graded(api)  # too late: the reservation is gone

    async with api.session_factory() as session:
        refunds = (
            (await session.execute(select(PaymentRow).where(PaymentRow.kind == "REFUND")))
            .scalars()
            .all()
        )
    assert [(r.amount, r.status) for r in refunds] == [(Decimal("200.00"), "SUCCEEDED")]
    assert (await api.http.get("/orders")).json()["items"] == []


async def test_a_farmer_cannot_pack_a_harvest_order(api):
    order = await graded_harvest_order(api)

    api.login(user(FARMER_A, Role.FARMER))
    response = await api.http.post(f"/orders/{order['id']}/pack")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"
