"""V2 harvest orders (issue #24): deposits, the harvest order, DELIVERY or BUYBACK, balance, refunds.

How a reservation becomes money and an order (HLD section 12):

    reservation.accepted   we remember the reservation: customer, farmer, deposit and agreed price
    POST /orders/payments/intent {kind: DEPOSIT, referenceId: <reservation id>}
                           the deposit is taken; `payment.succeeded` (kind DEPOSIT) tells land to
                           activate the reservation
    harvest.ready          the farmer harvested: a HARVEST order is created at once (status PLACED,
                           no option yet) for the harvested weight at the agreed price per kg, with
                           the plot as its pickup point. There is no quality gate (removed in
                           November 2026); the customer rates the order after delivery instead.
    POST /orders/harvest   the customer picks DELIVERY or BUYBACK
        DELIVERY -> the order goes to READY_FOR_DISPATCH (logistics pools it like any order);
                    when `delivery.completed` arrives the balance (total - deposit) is charged and
                    the order is COMPLETED (a deposit above the total is refunded instead)
        BUYBACK  -> the farmer sells the produce and credits the customer: the balance is charged
                    first (the customer owns the whole harvest), then the customer is credited
                    `buyback_rate` of its value (a REFUND payment) and the farmer is paid the rest;
                    the order is COMPLETED right away

Harvest orders skip packing (the farmer has already harvested), so `order.packed` is never published
for them. Every assumption made here is listed in docs/m1-assumptions.md under "Added by #24".
"""

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.auth import CurrentUser
from farm_common.errors import AppError, Conflict, NotFound
from order_service import delivery
from order_service import events as ev
from order_service.db import (
    CheckoutRow,
    HarvestRow,
    OrderEventRow,
    OrderItemRow,
    OrderRow,
    PaymentRow,
)
from order_service.payments import PaymentGateway, publish_payment_succeeded, refund_deposit
from order_service.schemas import (
    DeliveryMode,
    HarvestOption,
    HarvestOrderChoice,
    OrderDetail,
    OrderStatus,
    PaymentIntent,
    PaymentKind,
    PaymentStatus,
)
from order_service.service import (
    _LOAD,
    _delivery_date,
    _detail,
    _intent_schema,
    _load_order,
    _money,
    _now,
    _pick,
    _publish,
    _transition,
    _uid,
    _uuid,
    ready_note,
)
from order_service.settings import OrderSettings

logger = logging.getLogger(__name__)

S = OrderStatus
# Used when harvest.ready had no plot location. TODO(M1): ask land for it (GET /internal/geo/...).
UNKNOWN_LOCATION = {"type": "Point", "coordinates": [0.0, 0.0]}


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _crop_name(crop_id: str | None) -> str:
    return (crop_id or "crop").replace("_", " ").replace("-", " ").strip().title()


# --- consuming land events ---------------------------------------------------------------------


async def on_reservation_accepted(session: AsyncSession, payload: dict[str, Any]) -> None:
    """Remember who the reservation is for and how much deposit it asks.

    TODO(M1): the land contract only carries reservationId, customerId, farmerId and deposit. The
    price of the whole crop is read from `total` when land adds it; see docs/m1-assumptions.md.
    """
    reservation_id = _uuid(payload, "reservationId", "reservation_id")
    customer_id = _uuid(payload, "customerId", "customer_id")
    farmer_id = _uuid(payload, "farmerId", "farmer_id")
    if reservation_id is None or customer_id is None or farmer_id is None:
        logger.warning("Ignoring reservation.accepted without reservation, customer or farmer")
        return
    row = await session.get(HarvestRow, reservation_id, with_for_update=True)
    if row is None:
        row = HarvestRow(
            reservation_id=reservation_id,
            customer_id=customer_id,
            farmer_id=farmer_id,
            deposit=Decimal(0),
        )
        session.add(row)
    row.customer_id, row.farmer_id = customer_id, farmer_id
    row.deposit = _money(_decimal(_pick(payload, "deposit")) or Decimal(0))
    if (total := _decimal(_pick(payload, "total", "totalAmount", "total_amount"))) is not None:
        row.total = _money(total)
    row.crop_id = _pick(payload, "cropId", "crop_id") or row.crop_id
    row.expected_qty_kg = _decimal(_pick(payload, "expectedQtyKg", "expected_qty_kg")) or (
        row.expected_qty_kg
    )
    row.updated_at = _now()


async def on_harvest_ready(
    session: AsyncSession, payload: dict[str, Any], settings: OrderSettings
) -> None:
    """The farmer marked the harvest ready: create the HARVEST order for the harvested weight."""
    reservation_id = _uuid(payload, "reservationId", "reservation_id")
    customer_id = _uuid(payload, "customerId", "customer_id")
    farmer_id = _uuid(payload, "farmerId", "farmer_id")
    if reservation_id is None or customer_id is None or farmer_id is None:
        logger.warning("Ignoring harvest.ready without reservation, customer or farmer")
        return
    row = await session.get(HarvestRow, reservation_id, with_for_update=True)
    if row is None:  # reservation.accepted has not arrived (yet); events can cross
        row = HarvestRow(
            reservation_id=reservation_id,
            customer_id=customer_id,
            farmer_id=farmer_id,
            deposit=Decimal(0),
            deposit_paid=Decimal(0),
            deposit_refunded=Decimal(0),
        )
        session.add(row)
    if row.order_id is not None or row.status in ("ORDERED", "CANCELLED"):
        return  # a repeated event, or the reservation is gone
    row.status = "HARVEST_READY"
    row.crop_id = _pick(payload, "cropId", "crop_id") or row.crop_id
    row.expected_qty_kg = _decimal(_pick(payload, "expectedQtyKg", "expected_qty_kg")) or (
        row.expected_qty_kg
    )
    if (total := _decimal(_pick(payload, "total"))) is not None and row.total is None:
        row.total = _money(total)
    row.plot_location = _pick(payload, "plotCentroid", "plot_centroid") or row.plot_location
    row.updated_at = _now()
    weight = _decimal(_pick(payload, "harvestedQtyKg", "harvested_qty_kg"))
    await _create_harvest_order(session, row, weight, settings)


async def _create_harvest_order(
    session: AsyncSession,
    row: HarvestRow,
    weight: Decimal | None,
    settings: OrderSettings,
) -> None:
    """One HARVEST order with one line: the harvested weight at the agreed price per kg."""
    qty = weight if weight and weight > 0 else (row.expected_qty_kg or Decimal(1))
    if row.total is not None and row.expected_qty_kg:
        line_total = _money(row.total / row.expected_qty_kg * qty)
    elif row.total is not None:
        line_total = _money(row.total)
    else:
        # land did not send the price of the crop; estimate it from the deposit
        line_total = _money(row.deposit / settings.harvest_deposit_share)
        logger.warning(
            "Reservation %s has no total price, estimated %s", row.reservation_id, line_total
        )
    unit_price = _money(line_total / qty)

    now = _now()
    order_id, checkout_id = uuid4(), uuid4()
    checkout = CheckoutRow(
        id=checkout_id,
        customer_id=row.customer_id,
        payment_intent_id=row.deposit_payment_id or order_id,
        payment_status=(
            PaymentStatus.SUCCEEDED if row.deposit_paid > 0 else PaymentStatus.PENDING
        ).value,
        subtotal=line_total,
        delivery_fee=Decimal(0),
        total=line_total,
        currency="INR",
        created_at=now,
    )
    order = OrderRow(
        id=order_id,
        checkout_id=checkout_id,
        customer_id=row.customer_id,
        farm_id=None,
        farm_owner_id=row.farmer_id,
        order_type="HARVEST",
        reservation_id=row.reservation_id,
        harvest_option=None,
        status=S.PLACED.value,
        subtotal=line_total,
        delivery_fee=Decimal(0),
        total=line_total,
        currency="INR",
        delivery_address=None,
        delivery_date=None,
        delivery_mode=DeliveryMode.IMMEDIATE.value,
        created_at=now,
        updated_at=now,
    )
    order.items = [
        OrderItemRow(
            listing_id=None,
            farm_id=None,
            name=f"Harvest: {_crop_name(row.crop_id)}",
            unit="kg",
            quantity=qty,
            unit_price=unit_price,
            line_total=line_total,
            pickup_location=row.plot_location or UNKNOWN_LOCATION,
        )
    ]
    note = f"Harvest ready: {qty.normalize():f} kg. Choose delivery or buy-back."
    order.events = [OrderEventRow(status=S.PLACED.value, note=note, occurred_at=now)]
    checkout.orders.append(order)
    session.add(checkout)

    row.order_id = order_id
    row.status = "ORDERED"
    if row.deposit_payment_id is not None:
        await session.execute(
            update(PaymentRow)
            .where(PaymentRow.id == row.deposit_payment_id)
            .values(order_id=order_id)
        )
    _publish(session, ev.ORDER_PLACED, order)


async def on_reservation_cancelled(session: AsyncSession, payload: dict[str, Any]) -> None:
    """The reservation was cancelled before it became an order: give the deposit back.

    TODO(M1): `reservation.cancelled` is an assumed event name (land #18 owns it).
    """
    reservation_id = _uuid(payload, "reservationId", "reservation_id")
    row = (
        await session.get(HarvestRow, reservation_id, with_for_update=True)
        if reservation_id
        else None
    )
    if row is None or row.status in ("ORDERED", "CANCELLED"):
        return
    row.status = "CANCELLED"
    row.updated_at = _now()
    refund_deposit(session, row, None)


# --- deposit payments --------------------------------------------------------------------------


async def create_deposit_intent(
    session: AsyncSession,
    user: CurrentUser,
    amount: Decimal,
    reservation_id: UUID,
    gateway: PaymentGateway,
    settings: OrderSettings,
) -> PaymentIntent:
    """POST /orders/payments/intent with kind DEPOSIT. The mock gateway takes the money at once
    (unless `mock_auto_settle` is off, then POST /orders/payments/webhook settles it)."""
    row = await session.get(HarvestRow, reservation_id)
    if row is None or str(row.customer_id) != user.id:
        # same answer for a missing and a foreign reservation: ids cannot be probed
        raise NotFound("Reservation not found or not accepted yet", code="RESERVATION_NOT_FOUND")
    if row.status in ("ORDERED", "CANCELLED"):
        raise Conflict("This reservation can no longer be paid", code="RESERVATION_NOT_PAYABLE")
    if row.deposit_paid > 0:
        raise Conflict(
            "The deposit of this reservation is already paid", code="DEPOSIT_ALREADY_PAID"
        )
    if amount != row.deposit:
        raise AppError(
            "DEPOSIT_AMOUNT_MISMATCH",
            f"The deposit of this reservation is {row.deposit}",
            422,
            details={"deposit": float(row.deposit)},
        )
    intent = PaymentRow(
        id=uuid4(),
        customer_id=_uid(user),
        kind=PaymentKind.DEPOSIT.value,
        amount=amount,
        currency="INR",
        reference_id=reservation_id,
        status=PaymentStatus.PENDING.value,
    )
    session.add(intent)
    if settings.mock_auto_settle:
        await settle_payment(session, intent, await gateway.charge(amount, amount))
    await session.commit()
    return _intent_schema(intent)


async def settle_payment(session: AsyncSession, intent: PaymentRow, succeeded: bool) -> None:
    """Mark a DEPOSIT intent SUCCEEDED or FAILED; the caller commits.

    A repeated report with the same result changes nothing (a gateway may call the webhook twice).
    """
    target = PaymentStatus.SUCCEEDED if succeeded else PaymentStatus.FAILED
    if intent.status == target.value:
        return
    if intent.status != PaymentStatus.PENDING.value:
        raise Conflict(f"This payment is already {intent.status}", code="PAYMENT_ALREADY_SETTLED")
    if intent.kind != PaymentKind.DEPOSIT.value or intent.reference_id is None:
        raise Conflict(
            "Only deposit payments are settled here; a FULL payment is taken by POST /orders",
            code="PAYMENT_NOT_SETTLEABLE",
        )
    if not succeeded:
        intent.status = PaymentStatus.FAILED.value
        return
    row = await session.get(HarvestRow, intent.reference_id, with_for_update=True)
    if row is None or row.status == "CANCELLED":
        raise Conflict("This reservation can no longer be paid", code="RESERVATION_NOT_PAYABLE")
    if row.deposit_paid > 0:
        raise Conflict(
            "The deposit of this reservation is already paid", code="DEPOSIT_ALREADY_PAID"
        )
    intent.status = PaymentStatus.SUCCEEDED.value
    row.deposit_paid = intent.amount
    row.deposit_payment_id = intent.id
    row.updated_at = _now()
    publish_payment_succeeded(session, intent, row.reservation_id)


async def apply_webhook(
    session: AsyncSession, payment_intent_id: UUID, succeeded: bool
) -> PaymentIntent:
    intent = await session.get(PaymentRow, payment_intent_id, with_for_update=True)
    if intent is None:
        raise NotFound("Payment intent not found", code="PAYMENT_INTENT_NOT_FOUND")
    await settle_payment(session, intent, succeeded)
    await session.commit()
    return _intent_schema(intent)


# --- choosing delivery or buy-back -------------------------------------------------------------


async def choose_option(
    session: AsyncSession,
    user: CurrentUser,
    body: HarvestOrderChoice,
    settings: OrderSettings,
    gateway: PaymentGateway,
) -> OrderDetail:
    """POST /orders/harvest: the customer decides what happens to the harvest."""
    row = await session.get(HarvestRow, body.reservation_id, with_for_update=True)
    if row is None or str(row.customer_id) != user.id:
        raise NotFound("Reservation not found", code="RESERVATION_NOT_FOUND")
    if row.order_id is None:
        raise Conflict("The harvest is not ready yet", code="HARVEST_NOT_READY")
    order = await _load_order(session, row.order_id)
    if order.harvest_option is not None:
        if order.harvest_option == body.option.value:
            return await _detail(session, order.id)  # repeating the same choice is fine
        raise Conflict(
            "The option of this harvest was already chosen", code="HARVEST_OPTION_CHOSEN"
        )
    if order.status != S.PLACED:
        raise Conflict("This harvest order is closed", code="HARVEST_ORDER_CLOSED")

    order.harvest_option = body.option.value
    note = f"Customer chose {body.option.value.lower()}"
    if body.option == HarvestOption.DELIVERY:
        assert body.delivery_address is not None  # checked by the request model
        order.delivery_address = body.delivery_address.model_dump(mode="json", by_alias=True)
        order.delivery_date = _delivery_date(body.delivery_date)
        order.delivery_mode = body.delivery_mode.value
        order.delivery_fee = _money(delivery.fee_for(body.delivery_mode, settings))
        order.total = order.subtotal + order.delivery_fee
        await session.execute(
            update(CheckoutRow)
            .where(CheckoutRow.id == order.checkout_id)
            .values(delivery_fee=order.delivery_fee, total=order.total)
        )
        # the farmer has already harvested, so packing is only recorded; logistics gets the order
        await _transition(session, order, S.CONFIRMED, note=note)
        await _transition(session, order, S.PACKED, note="Harvest packed by the farmer")
        await _transition(
            session,
            order,
            S.READY_FOR_DISPATCH,
            ev.ORDER_READY_FOR_DISPATCH,
            note=ready_note(order),
        )
    else:
        # The customer owns the whole harvest, so the balance is due first; then the farmer sells
        # the produce and credits the customer `buyback_rate` of its value. The farmer keeps the rest.
        if not await _charge_balance(session, order, row, gateway):
            await session.rollback()  # nothing is decided: the customer can choose again
            raise Conflict(
                "The balance payment was declined; try again", code="BALANCE_PAYMENT_FAILED"
            )
        credit = _money(order.subtotal * settings.buyback_rate)
        session.add(
            PaymentRow(
                id=uuid4(),
                customer_id=order.customer_id,
                kind=PaymentKind.REFUND.value,
                amount=credit,
                currency=order.currency,
                reference_id=order.reservation_id,
                order_id=order.id,
                status=PaymentStatus.SUCCEEDED.value,
            )
        )
        if order.farm_owner_id is not None and order.subtotal > credit:
            session.add(
                PaymentRow(
                    id=uuid4(),
                    customer_id=order.customer_id,
                    kind=PaymentKind.PAYOUT.value,
                    amount=order.subtotal - credit,
                    currency=order.currency,
                    reference_id=order.farm_owner_id,
                    order_id=order.id,
                    status=PaymentStatus.SUCCEEDED.value,
                )
            )
        await _transition(session, order, S.CONFIRMED, note=note)
        await _transition(session, order, S.PACKED)
        await _transition(session, order, S.READY_FOR_DISPATCH)
        await _transition(session, order, S.DELIVERED, note="Bought back by the farmer")
        await _transition(
            session, order, S.COMPLETED, ev.ORDER_COMPLETED, note=f"Customer credited {credit}"
        )
    await session.commit()
    return await _detail(session, order.id, refresh=True)


# --- completing a delivered harvest order ------------------------------------------------------


async def _charge_balance(
    session: AsyncSession, order: OrderRow, row: HarvestRow | None, gateway: PaymentGateway
) -> bool:
    """Charge what is left of the harvest price after the deposit (a BALANCE payment), or refund a
    deposit larger than the price. Returns False when the gateway declined the balance."""
    held = row.deposit_paid - row.deposit_refunded if row else Decimal(0)
    balance = order.total - held
    if balance > 0:
        payment = PaymentRow(
            id=uuid4(),
            customer_id=order.customer_id,
            kind=PaymentKind.BALANCE.value,
            amount=balance,
            currency=order.currency,
            reference_id=order.reservation_id,
            order_id=order.id,
            status=PaymentStatus.PENDING.value,
        )
        session.add(payment)
        if not await gateway.charge(balance, balance):
            payment.status = PaymentStatus.FAILED.value
            return False
        payment.status = PaymentStatus.SUCCEEDED.value
        publish_payment_succeeded(session, payment, order.reservation_id, [order.id])
    elif balance < 0:
        # the deposit was more than the price of what was harvested: give the difference back
        session.add(
            PaymentRow(
                id=uuid4(),
                customer_id=order.customer_id,
                kind=PaymentKind.REFUND.value,
                amount=-balance,
                currency=order.currency,
                reference_id=order.reservation_id,
                order_id=order.id,
                status=PaymentStatus.SUCCEEDED.value,
            )
        )
    return True


async def complete_delivery(session: AsyncSession, order_id: UUID, gateway: PaymentGateway) -> None:
    """delivery.completed for a HARVEST order: settle the balance, then COMPLETED.

    If the balance is declined the order stays DELIVERED and a FAILED BALANCE payment is kept.
    TODO(M1): there is no retry endpoint yet; see docs/m1-assumptions.md.
    """
    order = (
        await session.execute(select(OrderRow).where(OrderRow.id == order_id).options(*_LOAD))
    ).scalar_one_or_none()
    if order is None or OrderStatus(order.status) not in {
        S.READY_FOR_DISPATCH,
        S.POOLED,
        S.IN_TRANSIT,
        S.DELIVERED,
    }:
        logger.info("Skipping delivery.completed for order %s", order_id)
        return
    if order.status != S.DELIVERED:
        await _transition(session, order, S.DELIVERED, note="Delivered")
    row = (
        await session.get(HarvestRow, order.reservation_id, with_for_update=True)
        if order.reservation_id
        else None
    )
    if not await _charge_balance(session, order, row, gateway):
        order.events.append(
            OrderEventRow(
                order_id=order.id, status=S.DELIVERED.value, note="BALANCE_PAYMENT_FAILED"
            )
        )
        return
    await _transition(session, order, S.COMPLETED, ev.ORDER_COMPLETED, note="Balance settled")
