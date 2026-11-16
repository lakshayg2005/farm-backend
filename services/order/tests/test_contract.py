import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from farm_common.auth import CurrentUser, Role, get_current_user
from order_service import stubs
from order_service.main import app
from order_service.schemas import Checkout, OrderDetail, OrderList, OrderStatus, PaymentIntent

SPEC_PATH = Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "order.json"

LOCATION = {"type": "Point", "coordinates": [75.7873, 26.9124]}
ADDRESS = {"line1": "12 Green Park", "city": "Jaipur", "pincode": "302001", "location": LOCATION}
CHECKOUT_BODY = {
    "items": [
        {"listingId": str(stubs.LISTING_ID), "qty": 2},
        {"listingId": str(stubs.LISTING_ID_2), "qty": 1.5},
    ],
    "deliveryAddress": ADDRESS,
    "paymentIntentId": str(stubs.PAYMENT_INTENT_ID),
}
IDEMPOTENCY = {"Idempotency-Key": "checkout-0001"}


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=str(stubs.CUSTOMER_ID), roles=[Role.CUSTOMER]
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_checkout_returns_one_order_per_farm_in_camel_case(client):
    response = client.post("/orders", json=CHECKOUT_BODY, headers=IDEMPOTENCY)

    assert response.status_code == 201
    body = response.json()
    assert {"customerId", "paymentIntentId", "paymentStatus", "deliveryFee"} <= set(body)
    checkout = Checkout.model_validate(body)
    assert len({o.farm_id for o in checkout.orders}) == 2
    assert checkout.total == sum(o.total for o in checkout.orders)


def test_checkout_requires_idempotency_key(client):
    response = client.post("/orders", json=CHECKOUT_BODY)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "change",
    [
        {"items": []},
        {"items": [{"listingId": str(uuid4()), "qty": 0}]},
        {"deliveryAddress": {**ADDRESS, "pincode": "12"}},
        {"deliveryAddress": {**ADDRESS, "location": {"type": "Point", "coordinates": [75, 123]}}},
        {"deliveryAddress": {k: v for k, v in ADDRESS.items() if k != "location"}},
        {"deliveryDate": "tomorrow"},
    ],
)
def test_checkout_rejects_invalid_body(client, change):
    response = client.post("/orders", json={**CHECKOUT_BODY, **change}, headers=IDEMPOTENCY)

    assert response.status_code == 422


def test_payment_intent(client):
    response = client.post("/orders/payments/intent", json={"amount": 499})

    assert response.status_code == 201
    intent = PaymentIntent.model_validate(response.json())
    assert intent.amount == 499
    assert intent.kind == "FULL"


def test_payment_intent_rejects_non_positive_amount(client):
    assert client.post("/orders/payments/intent", json={"amount": 0}).status_code == 422


def test_deposit_intents_are_allowed_but_refunds_are_not_requestable(client):
    deposit = client.post(
        "/orders/payments/intent",
        json={"amount": 5000, "kind": "DEPOSIT", "referenceId": str(uuid4())},
    )
    refund = client.post("/orders/payments/intent", json={"amount": 5, "kind": "REFUND"})

    assert deposit.status_code == 201
    assert deposit.json()["kind"] == "DEPOSIT"
    assert refund.status_code == 422


def test_checkout_accepts_a_delivery_date_and_orders_carry_pickup_points(client):
    body = {**CHECKOUT_BODY, "deliveryDate": "2026-11-20"}

    response = client.post("/orders", json=body, headers=IDEMPOTENCY)

    assert response.status_code == 201
    order = response.json()["orders"][0]
    assert order["deliveryDate"]
    assert order["items"][0]["pickupLocation"]["type"] == "Point"
    assert order["deliveryAddress"]["location"]["coordinates"] == [75.7873, 26.9124]


def test_list_orders_uses_cursor_pagination(client):
    response = client.get(
        "/orders", params={"status": "CONFIRMED", "role": "customer", "limit": 10}
    )

    assert response.status_code == 200
    assert "nextCursor" in response.json()
    OrderList.model_validate(response.json())


@pytest.mark.parametrize("params", [{"status": "SHIPPED"}, {"role": "admin"}, {"limit": 0}])
def test_list_orders_rejects_bad_filters(client, params):
    assert client.get("/orders", params=params).status_code == 422


def test_get_order_has_timeline(client):
    order_id = uuid4()

    response = client.get(f"/orders/{order_id}")

    assert response.status_code == 200
    detail = OrderDetail.model_validate(response.json())
    assert str(detail.id) == str(order_id)
    assert detail.events[0].status == OrderStatus.PLACED


def test_get_order_rejects_bad_id(client):
    assert client.get("/orders/not-a-uuid").status_code == 422


def test_rating(client):
    response = client.post(f"/orders/{uuid4()}/rating", json={"score": 5, "comment": "Fresh"})

    assert response.status_code == 201
    assert response.json()["score"] == 5
    assert client.post(f"/orders/{uuid4()}/rating", json={"score": 6}).status_code == 422


def test_requires_login():
    response = TestClient(app).get("/orders")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_only_customers_allowed():
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="u1", roles=[Role.DELIVERY])
    try:
        response = TestClient(app).get("/orders")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_committed_openapi_spec_is_current():
    """contracts/openapi/order.json must match the app. Regenerate it with:

    uv run --package farm-order python services/order/scripts/export_openapi.py
    """
    assert json.loads(SPEC_PATH.read_text()) == json.loads(json.dumps(app.openapi()))
