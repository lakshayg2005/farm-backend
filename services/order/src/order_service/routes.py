"""HTTP routes of the order service. Request and response models are in schemas.py (the contract)."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.auth import CurrentUser, Role, require_roles
from order_service import service
from order_service.deps import get_payment_gateway, get_session, get_stock_client
from order_service.payments import PaymentGateway
from order_service.schemas import (
    CancelRequest,
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
from order_service.settings import OrderSettings, get_order_settings
from order_service.stock import StockClient

router = APIRouter(tags=["orders"])

_customer = require_roles(Role.CUSTOMER)
_cancellers = require_roles(Role.CUSTOMER, Role.FARMER, Role.ADMIN)
_farmer_or_admin = require_roles(Role.FARMER, Role.ADMIN)
# customers see their orders, farmers the orders of their farms, admins all
_any_party = require_roles(Role.CUSTOMER, Role.FARMER, Role.ADMIN)

_AUTH_ERRORS: dict[int | str, dict] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid access token"},
    403: {"model": ErrorResponse, "description": "The token does not have the required role"},
}
_NOT_FOUND: dict[int | str, dict] = {
    404: {"model": ErrorResponse, "description": "ORDER_NOT_FOUND"},
}


@router.post(
    "/orders/payments/intent",
    response_model=PaymentIntent,
    status_code=201,
    summary="Create a mock payment intent",
    responses=_AUTH_ERRORS,
)
async def create_payment_intent(
    body: PaymentIntentCreate,
    user: CurrentUser = Depends(_customer),
    session: AsyncSession = Depends(get_session),
) -> PaymentIntent:
    return await service.create_payment_intent(session, user, body)


@router.post(
    "/orders",
    response_model=Checkout,
    status_code=201,
    summary="Checkout: place one order per farm",
    description=(
        "The cart lives in the app; its items are sent here. The Idempotency-Key header is "
        "required: repeating a request with the same key and body returns the first result "
        "instead of creating new orders. After a PAYMENT_FAILED (402) use a new key to retry."
    ),
    responses={
        **_AUTH_ERRORS,
        402: {"model": ErrorResponse, "description": "PAYMENT_FAILED"},
        404: {
            "model": ErrorResponse,
            "description": "PAYMENT_INTENT_NOT_FOUND or LISTING_NOT_FOUND",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "STOCK_UNAVAILABLE (details: listingId, requested, available), LISTING_NOT_AVAILABLE, "
                "PAYMENT_INTENT_USED, IDEMPOTENCY_KEY_REUSED or IDEMPOTENCY_IN_PROGRESS"
            ),
        },
        503: {"model": ErrorResponse, "description": "CATALOG_UNAVAILABLE"},
    },
)
async def checkout(
    body: CheckoutRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: CurrentUser = Depends(_customer),
    session: AsyncSession = Depends(get_session),
    stock: StockClient = Depends(get_stock_client),
    gateway: PaymentGateway = Depends(get_payment_gateway),
    settings: OrderSettings = Depends(get_order_settings),
) -> JSONResponse:
    status_code, content = await service.place_checkout(
        session, user, body, idempotency_key, stock, gateway, settings
    )
    return JSONResponse(content=content, status_code=status_code)


@router.get(
    "/orders",
    response_model=OrderList,
    summary="List orders, newest first (cursor pagination)",
    description=(
        "role=customer lists what I bought, role=farmer the orders of my farms. Without role, "
        "customers get their own orders, farmers the orders of their farms, admins all orders."
    ),
    responses=_AUTH_ERRORS,
)
async def list_orders(
    status: OrderStatus | None = None,
    role: Literal["customer", "farmer"] | None = None,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(_any_party),
    session: AsyncSession = Depends(get_session),
) -> OrderList:
    return await service.list_orders(session, user, status, role, cursor, limit)


@router.get(
    "/orders/{order_id}",
    response_model=OrderDetail,
    summary="Order with its status timeline",
    responses={**_AUTH_ERRORS, **_NOT_FOUND},
)
async def get_order(
    order_id: UUID,
    user: CurrentUser = Depends(_any_party),
    session: AsyncSession = Depends(get_session),
) -> OrderDetail:
    return await service.get_order(session, user, order_id)


@router.post(
    "/orders/{order_id}/pack",
    response_model=OrderDetail,
    summary="Farmer: mark a confirmed order as packed",
    description="Moves CONFIRMED to PACKED and publishes order.packed, which starts the inspection.",
    responses={
        **_AUTH_ERRORS,
        **_NOT_FOUND,
        409: {"model": ErrorResponse, "description": "INVALID_STATUS_TRANSITION"},
    },
)
async def pack_order(
    order_id: UUID,
    user: CurrentUser = Depends(_farmer_or_admin),
    session: AsyncSession = Depends(get_session),
) -> OrderDetail:
    return await service.pack_order(session, user, order_id)


@router.post(
    "/orders/{order_id}/cancel",
    response_model=OrderDetail,
    summary="Cancel an order before it is handed to logistics",
    description=(
        "The customer, the farm's owner or an admin can cancel. Publishes order.cancelled "
        "(catalog releases the stock) and records a REFUND payment."
    ),
    responses={
        **_AUTH_ERRORS,
        **_NOT_FOUND,
        409: {"model": ErrorResponse, "description": "ORDER_NOT_CANCELLABLE"},
    },
)
async def cancel_order(
    order_id: UUID,
    body: CancelRequest | None = None,
    user: CurrentUser = Depends(_cancellers),
    session: AsyncSession = Depends(get_session),
) -> OrderDetail:
    return await service.cancel_order(session, user, order_id, body.reason if body else None)


@router.post(
    "/orders/{order_id}/rating",
    response_model=OrderRating,
    status_code=201,
    summary="Rate a completed order's delivery",
    responses={
        **_AUTH_ERRORS,
        **_NOT_FOUND,
        409: {"model": ErrorResponse, "description": "ORDER_NOT_COMPLETED or ALREADY_RATED"},
    },
)
async def rate_order(
    order_id: UUID,
    body: OrderRatingCreate,
    user: CurrentUser = Depends(_customer),
    session: AsyncSession = Depends(get_session),
) -> OrderRating:
    return await service.rate_order(session, user, order_id, body)
