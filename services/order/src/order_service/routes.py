"""Stub routes of the order contract. They validate input and return example data only.

TODO(M1): #13 replaces the bodies with the real checkout saga; paths and models stay the same.
TODO(M1): auth uses farm_common.auth; only customers call these routes. Farmer and admin routes
(pack, cancel, fleet-wide listing) are added by #13.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query

from farm_common.auth import CurrentUser, Role, require_roles
from order_service import stubs
from order_service.schemas import (
    Checkout,
    CheckoutRequest,
    ErrorResponse,
    OrderDetail,
    OrderList,
    OrderRating,
    OrderRatingCreate,
    OrderStatus,
    PaymentIntent,
    PaymentIntentCreate,
)

router = APIRouter(tags=["orders"])

_customer = require_roles(Role.CUSTOMER)

_AUTH_ERRORS: dict[int | str, dict] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid access token"},
    403: {"model": ErrorResponse, "description": "The token does not have the customer role"},
}


@router.post(
    "/orders/payments/intent",
    response_model=PaymentIntent,
    status_code=201,
    summary="Create a mock payment intent",
    responses=_AUTH_ERRORS,
)
async def create_payment_intent(
    body: PaymentIntentCreate, user: CurrentUser = Depends(_customer)
) -> PaymentIntent:
    return stubs.sample_payment_intent(body.amount, body.kind, body.reference_id)


@router.post(
    "/orders",
    response_model=Checkout,
    status_code=201,
    summary="Checkout: place one order per farm",
    description=(
        "The cart lives in the app; its items are sent here. The Idempotency-Key header is "
        "required: repeating a request with the same key returns the first result instead of "
        "creating new orders."
    ),
    responses={
        **_AUTH_ERRORS,
        402: {"model": ErrorResponse, "description": "PAYMENT_FAILED"},
        409: {
            "model": ErrorResponse,
            "description": "STOCK_UNAVAILABLE: details lists the listing ids that ran out",
        },
    },
)
async def checkout(
    body: CheckoutRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: CurrentUser = Depends(_customer),
) -> Checkout:
    return stubs.sample_checkout()


@router.get(
    "/orders",
    response_model=OrderList,
    summary="List my orders, newest first (cursor pagination)",
    responses=_AUTH_ERRORS,
)
async def list_orders(
    status: OrderStatus | None = None,
    role: Literal["customer", "farmer"] | None = None,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(_customer),
) -> OrderList:
    return stubs.sample_order_list()


@router.get(
    "/orders/{order_id}",
    response_model=OrderDetail,
    summary="Order with its status timeline",
    responses={**_AUTH_ERRORS, 404: {"model": ErrorResponse, "description": "NOT_FOUND"}},
)
async def get_order(order_id: UUID, user: CurrentUser = Depends(_customer)) -> OrderDetail:
    return stubs.sample_order_detail(order_id)


@router.post(
    "/orders/{order_id}/rating",
    response_model=OrderRating,
    status_code=201,
    summary="Rate a completed order's delivery",
    responses={
        **_AUTH_ERRORS,
        404: {"model": ErrorResponse, "description": "NOT_FOUND"},
        409: {
            "model": ErrorResponse,
            "description": "ORDER_NOT_COMPLETED or ALREADY_RATED",
        },
    },
)
async def rate_order(
    order_id: UUID, body: OrderRatingCreate, user: CurrentUser = Depends(_customer)
) -> OrderRating:
    return stubs.sample_rating(order_id, body.score, body.comment)
