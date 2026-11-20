"""Business logic of the order service: checkout saga, status changes, queries and event handlers.

Checkout saga (POST /orders):
    1. claim the Idempotency-Key (a repeat returns the first result)
    2. look up each listing's farm in catalog and give every per-farm order its id
    3. reserve stock in catalog, one order at a time (catalog keys a reservation by order id)
    4. take the mock payment
    5. save checkout + one order per farm + timeline + outbox events in ONE transaction
       - payment ok     -> orders CONFIRMED, `order.placed` published
       - payment failed -> orders CANCELLED, `order.cancelled` published (catalog releases stock)
    Any failure before step 5 releases every order id that was tried.

Harvest orders (V2) are in harvest.py; event handlers are in consumers.py.

Every status change is a guarded UPDATE (`WHERE status = <expected>`), writes an order_events row
and an outbox event in the same transaction as the change.
"""

import base64
import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from farm_common.auth import CurrentUser, Role
from farm_common.errors import AppError, Conflict, Forbidden, NotFound, Unauthorized, error_body
from farm_common.events import Event
from farm_common.outbox import add_event
from order_service import events as ev
from order_service.db import (
    CheckoutRow,
    IdempotencyKeyRow,
    OrderEventRow,
    OrderItemRow,
    OrderRow,
    OutboxEvent,
    PaymentRow,
    RatingRow,
)
from order_service.payments import PaymentGateway, refund_harvest_order
from order_service.schemas import (
    Checkout,
    CheckoutRequest,
    DeliveryAddress,
    GeoPoint,
    Order,
    OrderDetail,
    OrderEvent,
    OrderItem,
    OrderList,
    OrderRating,
    OrderRatingCreate,
    OrderStatus,
    OrderSummary,
    PaymentIntent,
    PaymentIntentCreate,
    PaymentKind,
    PaymentRecord,
    PaymentStatus,
    Payout,
    PayoutList,
)
from order_service.settings import OrderSettings
from order_service.stock import ReservedLine, StockClient, StockRequestItem

logger = logging.getLogger(__name__)

S = OrderStatus

# Allowed status changes (HLD section 11). After READY_FOR_DISPATCH logistics events drive the order;
# steps may be skipped when an intermediate event never arrives (TODO(M1): see docs/m1-assumptions.md).
ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    S.PLACED: {S.CONFIRMED, S.CANCELLED},
    S.CONFIRMED: {S.PACKED, S.CANCELLED},
    S.PACKED: {S.QC_PASSED, S.CANCELLED},
    S.QC_PASSED: {S.READY_FOR_DISPATCH, S.CANCELLED},
    S.READY_FOR_DISPATCH: {S.POOLED, S.IN_TRANSIT, S.DELIVERED},
    S.POOLED: {S.IN_TRANSIT, S.DELIVERED},
    S.IN_TRANSIT: {S.DELIVERED},
    S.DELIVERED: {S.COMPLETED},
    S.COMPLETED: set(),
    S.CANCELLED: set(),
}
# A customer can cancel until the order has been handed to logistics.
CANCELLABLE = {S.PLACED, S.CONFIRMED, S.PACKED, S.QC_PASSED}

_CENT = Decimal("0.01")
_IST = ZoneInfo("Asia/Kolkata")
MAX_DAYS_AHEAD = 30


def _today() -> date:
    return datetime.now(_IST).date()


def _delivery_date(requested: date | None) -> date:
    """Tomorrow unless the customer chose a day within the next 30 days."""
    today = _today()
    if requested is None:
        return today + timedelta(days=1)
    if requested < today or requested > today + timedelta(days=MAX_DAYS_AHEAD):
        raise AppError(
            "INVALID_DELIVERY_DATE",
            f"Choose a delivery date between today and {MAX_DAYS_AHEAD} days from now",
            422,
        )
    return requested


def _now() -> datetime:
    return datetime.now(UTC)


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _uid(user: CurrentUser) -> UUID:
    try:
        return UUID(user.id)
    except ValueError as exc:
        raise Unauthorized("Invalid token", code="INVALID_TOKEN") from exc


def _pick(payload: dict[str, Any], *names: str) -> Any:
    """First present key; payloads are camelCase but a snake_case spelling is tolerated."""
    for name in names:
        if payload.get(name) is not None:
            return payload[name]
    return None


def _uuid(payload: dict[str, Any], *names: str) -> UUID | None:
    value = _pick(payload, *names)
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        logger.warning("Ignoring invalid id %r in event payload", value)
        return None


# --- mapping rows to API models ----------------------------------------------------------------


def _order_schema(row: OrderRow) -> Order:
    return Order(
        id=row.id,
        checkout_id=row.checkout_id,
        order_type=row.order_type,  # type: ignore[arg-type]
        farm_id=row.farm_id,
        reservation_id=row.reservation_id,
        harvest_option=row.harvest_option,  # type: ignore[arg-type]
        status=OrderStatus(row.status),
        items=[
            OrderItem(
                listing_id=i.listing_id,
                farm_id=i.farm_id,
                name=i.name,
                unit=i.unit,
                qty=float(i.quantity),
                unit_price=float(i.unit_price),
                line_total=float(i.line_total),
                pickup_location=GeoPoint.model_validate(i.pickup_location),
            )
            for i in row.items
        ],
        subtotal=float(row.subtotal),
        delivery_fee=float(row.delivery_fee),
        total=float(row.total),
        currency=row.currency,
        delivery_address=(
            DeliveryAddress.model_validate(row.delivery_address) if row.delivery_address else None
        ),
        delivery_date=row.delivery_date,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _balance_due(row: OrderRow, payments: list[PaymentRow]) -> float:
    """HARVEST orders that are delivered (or not decided yet): total minus what was paid."""
    if (
        row.order_type != "HARVEST"
        or row.harvest_option == "BUYBACK"
        or row.status in (S.COMPLETED, S.CANCELLED)
    ):
        return 0.0
    paid = sum(
        (
            p.amount
            for p in payments
            if p.status == PaymentStatus.SUCCEEDED and p.kind in ("DEPOSIT", "BALANCE")
        ),
        Decimal(0),
    )
    return float(max(row.total - paid, Decimal(0)))


def _detail_schema(row: OrderRow, payments: list[PaymentRow]) -> OrderDetail:
    rating = (
        OrderRating(
            order_id=row.rating.order_id,
            score=row.rating.score,
            comment=row.rating.comment,
            created_at=row.rating.created_at,
        )
        if row.rating
        else None
    )
    return OrderDetail(
        **_order_schema(row).model_dump(),
        events=[
            OrderEvent(status=OrderStatus(e.status), occurred_at=e.occurred_at, note=e.note)
            for e in row.events
        ],
        rating=rating,
        payments=[
            PaymentRecord(
                id=p.id,
                kind=PaymentKind(p.kind),
                amount=float(p.amount),
                currency=p.currency,
                status=PaymentStatus(p.status),
                created_at=p.created_at,
            )
            for p in payments
        ],
        balance_due=_balance_due(row, payments),
    )


def _intent_schema(row: PaymentRow) -> PaymentIntent:
    return PaymentIntent(
        id=row.id,
        amount=float(row.amount),
        currency=row.currency,
        kind=PaymentKind(row.kind),
        reference_id=row.reference_id,
        status=PaymentStatus(row.status),
        created_at=row.created_at,
    )


def _checkout_schema(row: CheckoutRow) -> Checkout:
    return Checkout(
        id=row.id,
        customer_id=row.customer_id,
        payment_intent_id=row.payment_intent_id,
        payment_status=PaymentStatus(row.payment_status),
        subtotal=float(row.subtotal),
        delivery_fee=float(row.delivery_fee),
        total=float(row.total),
        currency=row.currency,
        orders=[_order_schema(o) for o in row.orders],
        created_at=row.created_at,
    )


# --- loading and access ------------------------------------------------------------------------

_LOAD = (
    selectinload(OrderRow.items),
    selectinload(OrderRow.events),
    selectinload(OrderRow.rating),
)


async def _load_order(session: AsyncSession, order_id: UUID, refresh: bool = False) -> OrderRow:
    stmt = select(OrderRow).where(OrderRow.id == order_id).options(*_LOAD)
    if refresh:
        stmt = stmt.execution_options(populate_existing=True)
    order = (await session.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise NotFound("Order not found", code="ORDER_NOT_FOUND")
    return order


async def _detail_of(session: AsyncSession, row: OrderRow) -> OrderDetail:
    """The order with its timeline and its payments (a checkout's FULL payment, deposits, refunds)."""
    belongs = PaymentRow.order_id == row.id
    if row.order_type == "PRODUCE":
        belongs = or_(
            belongs,
            and_(
                PaymentRow.kind == PaymentKind.FULL.value,
                PaymentRow.reference_id == row.checkout_id,
            ),
        )
    payments = list(
        (
            await session.execute(
                select(PaymentRow).where(belongs).order_by(PaymentRow.created_at, PaymentRow.id)
            )
        ).scalars()
    )
    return _detail_schema(row, payments)


async def _detail(session: AsyncSession, order_id: UUID, refresh: bool = False) -> OrderDetail:
    return await _detail_of(session, await _load_order(session, order_id, refresh=refresh))


def _can_view(order: OrderRow, user: CurrentUser) -> bool:
    if user.has_any(Role.ADMIN):
        return True
    if user.has_any(Role.CUSTOMER) and str(order.customer_id) == user.id:
        return True
    return (
        user.has_any(Role.FARMER)
        and order.farm_owner_id is not None
        and (str(order.farm_owner_id) == user.id)
    )


async def _load_visible(session: AsyncSession, order_id: UUID, user: CurrentUser) -> OrderRow:
    order = await _load_order(session, order_id)
    if not _can_view(order, user):
        # same answer as a missing order, so ids of other people's orders cannot be probed
        raise NotFound("Order not found", code="ORDER_NOT_FOUND")
    return order


# --- status changes ----------------------------------------------------------------------------


async def _transition(
    session: AsyncSession,
    order: OrderRow,
    new: OrderStatus,
    event_type: str | None = None,
    note: str | None = None,
    **extra: Any,
) -> None:
    """Move `order` to `new` with a guarded UPDATE, record the timeline entry and the event.

    The caller commits. Raises Conflict if the move is not allowed or the order changed meanwhile.
    """
    current = OrderStatus(order.status)
    if new not in ALLOWED[current]:
        raise Conflict(
            f"Order cannot move from {current} to {new}", code="INVALID_STATUS_TRANSITION"
        )
    now = _now()
    result = await session.execute(
        update(OrderRow)
        .where(OrderRow.id == order.id, OrderRow.status == current.value)
        .values(status=new.value, updated_at=now)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise Conflict("The order was changed by someone else, reload it", code="ORDER_CHANGED")
    order.status = new.value
    order.updated_at = now
    session.add(OrderEventRow(order_id=order.id, status=new.value, note=note, occurred_at=now))
    if new == OrderStatus.COMPLETED:
        _record_payout(session, order)
    if event_type:
        _publish(session, event_type, order, **extra)


def _record_payout(session: AsyncSession, order: OrderRow) -> None:
    """The farmer is paid the item value (not the delivery fee) when an order completes. A buy-back
    harvest order pays the customer instead, so it records no farmer payout."""
    if order.farm_owner_id is None or order.harvest_option == "BUYBACK":
        return
    session.add(
        PaymentRow(
            id=uuid4(),
            customer_id=order.customer_id,
            kind=PaymentKind.PAYOUT.value,
            amount=order.subtotal,
            currency=order.currency,
            reference_id=order.farm_owner_id,
            order_id=order.id,
            status=PaymentStatus.SUCCEEDED.value,
        )
    )


async def list_payouts(session: AsyncSession, user: CurrentUser, limit: int) -> PayoutList:
    query = select(PaymentRow).where(PaymentRow.kind == PaymentKind.PAYOUT.value)
    if not user.has_any(Role.ADMIN):
        query = query.where(PaymentRow.reference_id == UUID(user.id))
    rows = (
        (await session.execute(query.order_by(PaymentRow.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    items = [
        Payout(
            order_id=r.order_id, amount=float(r.amount), currency=r.currency, paid_at=r.created_at
        )
        for r in rows
        if r.order_id is not None
    ]
    return PayoutList(items=items, total=round(sum(i.amount for i in items), 2))


def _publish(session: AsyncSession, event_type: str, order: OrderRow, **extra: Any) -> None:
    add_event(
        session,
        OutboxEvent,
        Event(type=event_type, producer=ev.PRODUCER, payload=ev.build_payload(order, **extra)),
    )


async def pack_order(session: AsyncSession, user: CurrentUser, order_id: UUID) -> OrderDetail:
    order = await _load_order(session, order_id)
    is_admin = user.has_any(Role.ADMIN)
    # TODO(M1): farm_owner_id comes from catalog's reserve response (assumed); if catalog does not
    # send it, any farmer can pack. Tighten once catalog is merged.
    if not is_admin and order.farm_owner_id is not None and str(order.farm_owner_id) != user.id:
        raise Forbidden("This order belongs to another farm")
    await _transition(session, order, S.PACKED, ev.ORDER_PACKED)
    await session.commit()
    return await _detail(session, order_id, refresh=True)


async def cancel_order(
    session: AsyncSession, user: CurrentUser, order_id: UUID, reason: str | None
) -> OrderDetail:
    """The customer, the farm's owner (HLD: farmers can cancel too) or an admin cancels an order
    until it is handed to logistics. A REFUND payment is recorded and catalog frees the stock."""
    order = await _load_order(session, order_id)
    actor: Literal["customer", "farmer", "admin", "system"]
    if user.has_any(Role.ADMIN):
        actor = "admin"
    elif user.has_any(Role.CUSTOMER) and str(order.customer_id) == user.id:
        actor = "customer"
    elif user.has_any(Role.FARMER) and order.farm_owner_id and str(order.farm_owner_id) == user.id:
        actor = "farmer"
    else:
        raise NotFound("Order not found", code="ORDER_NOT_FOUND")
    if OrderStatus(order.status) not in CANCELLABLE:
        raise Conflict(
            f"An order in status {order.status} can no longer be cancelled",
            code="ORDER_NOT_CANCELLABLE",
        )
    await cancel_row(session, order, actor, reason)
    await session.commit()
    return await _detail(session, order_id, refresh=True)


async def cancel_row(
    session: AsyncSession,
    order: OrderRow,
    actor: Literal["customer", "farmer", "admin", "system"],
    reason: str | None,
) -> None:
    """Cancel an order (the caller commits): timeline, `order.cancelled` and the REFUND payment.

    A produce order is refunded in full; a harvest order gets its deposit back (nothing else was
    paid). The caller checks that the order may be cancelled.
    """
    default_reason = {
        "customer": "CUSTOMER_REQUEST",
        "farmer": "FARMER_CANCELLED",
        "system": "SYSTEM_CANCELLED",
    }.get(actor, "ADMIN_CANCELLED")
    if order.order_type == "HARVEST":
        refund = await refund_harvest_order(session, order)
    else:
        refund = order.total
        session.add(
            PaymentRow(
                id=uuid4(),
                customer_id=order.customer_id,
                kind=PaymentKind.REFUND.value,
                amount=order.total,
                currency=order.currency,
                reference_id=order.checkout_id,
                order_id=order.id,
                status=PaymentStatus.SUCCEEDED.value,
            )
        )
    await _transition(
        session,
        order,
        S.CANCELLED,
        ev.ORDER_CANCELLED,
        note=reason,
        reason=reason or default_reason,
        cancelled_by=actor,
        refund_amount=float(refund),
    )


# --- queries -----------------------------------------------------------------------------------


def _encode_cursor(row: OrderRow) -> str:
    raw = json.dumps({"t": row.created_at.isoformat(), "i": str(row.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(data["t"]), UUID(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AppError("INVALID_CURSOR", "The cursor is not valid", 400) from exc


async def list_orders(
    session: AsyncSession,
    user: CurrentUser,
    status: OrderStatus | None,
    role: Literal["customer", "farmer"] | None,
    cursor: str | None,
    limit: int,
) -> OrderList:
    """Newest first, cursor paginated.

    `role=customer` lists what I bought, `role=farmer` the orders of my farms. Without `role`,
    customers get their own orders, farmers the orders of their farms, admins everything.
    """
    me = _uid(user)
    conditions: list[Any] = []
    if role == "customer":
        conditions.append(OrderRow.customer_id == me)
    elif role == "farmer":
        conditions.append(OrderRow.farm_owner_id == me)
    elif not user.has_any(Role.ADMIN):
        own = []
        if user.has_any(Role.CUSTOMER):
            own.append(OrderRow.customer_id == me)
        if user.has_any(Role.FARMER):
            own.append(OrderRow.farm_owner_id == me)
        conditions.append(or_(*own))
    if status:
        conditions.append(OrderRow.status == status.value)
    if cursor:
        at, last_id = _decode_cursor(cursor)
        conditions.append(
            or_(OrderRow.created_at < at, and_(OrderRow.created_at == at, OrderRow.id < last_id))
        )

    rows = list(
        (
            await session.execute(
                select(OrderRow)
                .where(*conditions)
                .options(selectinload(OrderRow.items))
                .order_by(OrderRow.created_at.desc(), OrderRow.id.desc())
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    more = len(rows) > limit
    page = rows[:limit]
    return OrderList(
        items=[
            OrderSummary(
                id=r.id,
                checkout_id=r.checkout_id,
                order_type=r.order_type,  # type: ignore[arg-type]
                farm_id=r.farm_id,
                reservation_id=r.reservation_id,
                harvest_option=r.harvest_option,  # type: ignore[arg-type]
                status=OrderStatus(r.status),
                item_count=len(r.items),
                total=float(r.total),
                currency=r.currency,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in page
        ],
        next_cursor=_encode_cursor(page[-1]) if more else None,
    )


async def get_order(session: AsyncSession, user: CurrentUser, order_id: UUID) -> OrderDetail:
    return await _detail_of(session, await _load_visible(session, order_id, user))


# --- payments and rating -----------------------------------------------------------------------


async def create_payment_intent(
    session: AsyncSession, user: CurrentUser, body: PaymentIntentCreate
) -> PaymentIntent:
    """A FULL intent for checkout. DEPOSIT intents are created by harvest.create_deposit_intent."""
    row = PaymentRow(
        id=uuid4(),
        customer_id=_uid(user),
        amount=_money(Decimal(str(body.amount))),
        currency="INR",
        kind=body.kind.value,
        reference_id=body.reference_id,
        status=PaymentStatus.PENDING.value,
    )
    session.add(row)
    await session.commit()
    return _intent_schema(row)


async def rate_order(
    session: AsyncSession, user: CurrentUser, order_id: UUID, body: OrderRatingCreate
) -> OrderRating:
    order = await _load_order(session, order_id)
    if str(order.customer_id) != user.id:
        raise NotFound("Order not found", code="ORDER_NOT_FOUND")
    if order.status != S.COMPLETED:
        raise Conflict("Only a completed order can be rated", code="ORDER_NOT_COMPLETED")
    if order.rating is not None:
        raise Conflict("This order was already rated", code="ALREADY_RATED")
    row = RatingRow(
        order_id=order.id, customer_id=order.customer_id, score=body.score, comment=body.comment
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("This order was already rated", code="ALREADY_RATED") from exc
    await session.refresh(row)
    return OrderRating(
        order_id=row.order_id, score=row.score, comment=row.comment, created_at=row.created_at
    )


# --- checkout saga -----------------------------------------------------------------------------


def _request_hash(body: CheckoutRequest) -> str:
    return hashlib.sha256(body.model_dump_json().encode()).hexdigest()


async def place_checkout(
    session: AsyncSession,
    user: CurrentUser,
    body: CheckoutRequest,
    idempotency_key: str,
    stock: StockClient,
    gateway: PaymentGateway,
    settings: OrderSettings,
) -> tuple[int, dict[str, Any]]:
    """Run the checkout saga. Returns (HTTP status, JSON body) so repeats can be replayed as is."""
    customer_id = _uid(user)
    request_hash = _request_hash(body)

    existing = await session.get(IdempotencyKeyRow, (customer_id, idempotency_key))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise Conflict(
                "This Idempotency-Key was already used with a different request",
                code="IDEMPOTENCY_KEY_REUSED",
            )
        if existing.state == "DONE" and existing.response_body is not None:
            return existing.response_status or 200, existing.response_body
        raise Conflict("This checkout is still being processed", code="IDEMPOTENCY_IN_PROGRESS")

    claim = IdempotencyKeyRow(
        customer_id=customer_id,
        key=idempotency_key,
        request_hash=request_hash,
        state="IN_PROGRESS",
    )
    session.add(claim)
    try:
        await session.commit()
    except IntegrityError as exc:  # a parallel request with the same key won the race
        await session.rollback()
        raise Conflict(
            "This checkout is still being processed", code="IDEMPOTENCY_IN_PROGRESS"
        ) from exc

    try:
        return await _run_checkout(session, customer_id, body, claim, stock, gateway, settings)
    except Exception:
        # nothing was saved: forget the key so the customer can retry with the same one
        await session.rollback()
        await session.execute(
            delete(IdempotencyKeyRow).where(
                IdempotencyKeyRow.customer_id == customer_id,
                IdempotencyKeyRow.key == idempotency_key,
            )
        )
        await session.commit()
        raise


async def _run_checkout(
    session: AsyncSession,
    customer_id: UUID,
    body: CheckoutRequest,
    claim: IdempotencyKeyRow,
    stock: StockClient,
    gateway: PaymentGateway,
    settings: OrderSettings,
) -> tuple[int, dict[str, Any]]:
    intent = await session.get(PaymentRow, body.payment_intent_id)
    if intent is None or intent.customer_id != customer_id:
        raise NotFound("Payment intent not found", code="PAYMENT_INTENT_NOT_FOUND")
    if intent.status != PaymentStatus.PENDING or intent.kind != PaymentKind.FULL:
        raise Conflict("This payment intent cannot be used", code="PAYMENT_INTENT_USED")

    delivery_date = _delivery_date(body.delivery_date)

    # the same listing twice in the cart is one line
    wanted: dict[UUID, Decimal] = {}
    for item in body.items:
        wanted[item.listing_id] = wanted.get(item.listing_id, Decimal(0)) + Decimal(str(item.qty))

    # one order per farm; its id is the reservation key in catalog
    farms = await stock.farms_of(list(wanted))
    by_farm: dict[UUID, list[StockRequestItem]] = {}
    for listing_id, qty in wanted.items():
        by_farm.setdefault(farms[listing_id], []).append(StockRequestItem(listing_id, qty))
    order_ids = {farm_id: uuid4() for farm_id in by_farm}

    reserved: dict[UUID, list[ReservedLine]] = {}
    attempted: list[UUID] = []
    try:
        for farm_id, items in by_farm.items():
            attempted.append(order_ids[farm_id])
            lines = await stock.reserve(order_ids[farm_id], items)
            if {line.listing_id for line in lines} != {i.listing_id for i in items} or any(
                line.farm_id != farm_id for line in lines
            ):
                raise AppError("CATALOG_ERROR", "Stock reply did not match the cart", 502)
            reserved[farm_id] = lines
        return await _save_checkout(
            session,
            customer_id,
            body,
            delivery_date,
            claim,
            intent,
            order_ids,
            reserved,
            gateway,
            settings,
        )
    except Exception:
        await session.rollback()
        # also release the order that failed: catalog may have reserved before the error
        await _release_quietly(stock, attempted)
        raise


async def _release_quietly(stock: StockClient, order_ids: list[UUID]) -> None:
    for order_id in order_ids:
        try:
            await stock.release(order_id)
        except Exception:
            # TODO(M1): needs a reconciliation job if this ever happens in practice
            logger.exception("Could not release stock for order %s", order_id)


async def _save_checkout(
    session: AsyncSession,
    customer_id: UUID,
    body: CheckoutRequest,
    delivery_date: date,
    claim: IdempotencyKeyRow,
    intent: PaymentRow,
    order_ids: dict[UUID, UUID],
    reserved: dict[UUID, list[ReservedLine]],
    gateway: PaymentGateway,
    settings: OrderSettings,
) -> tuple[int, dict[str, Any]]:
    fee = _money(settings.delivery_fee_per_order)
    subtotals = {
        farm_id: _money(sum((_money(line.unit_price * line.qty) for line in lines), Decimal(0)))
        for farm_id, lines in reserved.items()
    }
    subtotal = sum(subtotals.values(), Decimal(0))
    total = subtotal + fee * len(reserved)

    paid = await gateway.charge(intent.amount, total)
    final = S.CONFIRMED if paid else S.CANCELLED
    now = _now()
    address = body.delivery_address.model_dump(mode="json", by_alias=True)
    checkout_id = uuid4()

    checkout = CheckoutRow(
        id=checkout_id,
        customer_id=customer_id,
        payment_intent_id=intent.id,
        payment_status=(PaymentStatus.SUCCEEDED if paid else PaymentStatus.FAILED).value,
        subtotal=subtotal,
        delivery_fee=fee * len(reserved),
        total=total,
        currency="INR",
        created_at=now,
    )
    session.add(checkout)
    for farm_id, lines in reserved.items():
        order = OrderRow(
            id=order_ids[farm_id],
            checkout_id=checkout_id,
            customer_id=customer_id,
            farm_id=farm_id,
            farm_owner_id=lines[0].farm_owner_id,
            order_type="PRODUCE",
            status=final.value,
            subtotal=subtotals[farm_id],
            delivery_fee=fee,
            total=subtotals[farm_id] + fee,
            currency="INR",
            delivery_address=address,
            delivery_date=delivery_date,
            notes=body.notes,
            created_at=now,
            updated_at=now,
        )
        order.items = [
            OrderItemRow(
                listing_id=line.listing_id,
                farm_id=line.farm_id,
                name=line.name,
                unit=line.unit,
                quantity=line.qty,
                unit_price=line.unit_price,
                line_total=_money(line.unit_price * line.qty),
                pickup_location=line.pickup_location,
            )
            for line in lines
        ]
        order.events = [
            OrderEventRow(status=S.PLACED.value, occurred_at=now),
            OrderEventRow(
                status=final.value,
                note="Payment received" if paid else "PAYMENT_FAILED",
                occurred_at=now,
            ),
        ]
        checkout.orders.append(order)
        if paid:
            _publish(session, ev.ORDER_PLACED, order)
        else:
            _publish(
                session,
                ev.ORDER_CANCELLED,
                order,
                reason="PAYMENT_FAILED",
                cancelled_by="system",
                refund_amount=0.0,
            )

    intent.status = (PaymentStatus.SUCCEEDED if paid else PaymentStatus.FAILED).value
    intent.amount = total  # the mock gateway charges the real total
    intent.reference_id = checkout_id

    if paid:
        add_event(
            session,
            OutboxEvent,
            Event(
                type=ev.PAYMENT_SUCCEEDED,
                producer=ev.PRODUCER,
                payload=ev.PaymentSucceededPayload(
                    payment_id=intent.id,
                    kind=PaymentKind.FULL,
                    amount=float(total),
                    currency="INR",
                    customer_id=customer_id,
                    reference_id=checkout_id,
                    checkout_id=checkout_id,
                    order_ids=[o.id for o in checkout.orders],
                ).model_dump(mode="json", by_alias=True),
            ),
        )
        status_code = 201
        response = jsonable_encoder(_checkout_schema(checkout))
    else:
        status_code = 402
        response = error_body(
            "PAYMENT_FAILED",
            "The payment was declined; stock was released. Start a new checkout to retry.",
            {"checkoutId": str(checkout_id)},
        )
    claim.state = "DONE"
    claim.response_status = status_code
    claim.response_body = response
    await session.commit()
    return status_code, response
