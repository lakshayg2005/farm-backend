"""Event consumers: maps domain events from RabbitMQ to user notifications and pushes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from notification_service.db import DeviceToken, Notification, ProcessedEvent
from notification_service.push import send_push_notification
from notification_service.schemas import NotificationType
from notification_service.settings import NotificationSettings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def map_event_to_notification(
    event: Any,
) -> tuple[UUID, NotificationType, str, str, dict[str, Any]] | None:
    """Map a domain event into (target_user_id, notification_type, title, body, payload).

    Returns None if the event does not map to a user notification.
    """
    p = getattr(event, "payload", {}) if not isinstance(event, dict) else event.get("payload", {})
    etype = getattr(event, "type", "") if not isinstance(event, dict) else event.get("type", "")

    # 1. Order events
    if etype == "order.placed":
        cid = p.get("customerId")
        if not cid:
            return None
        oid = str(p.get("orderId", ""))
        short_id = oid[:8] if oid else ""
        return (
            UUID(str(cid)),
            NotificationType.ORDER_PLACED,
            "Order Placed Successfully",
            f"Your order #{short_id} has been placed.",
            {"orderId": oid, "total": p.get("total")},
        )

    if etype == "order.confirmed":
        cid = p.get("customerId")
        if not cid:
            return None
        oid = str(p.get("orderId", ""))
        short_id = oid[:8] if oid else ""
        return (
            UUID(str(cid)),
            NotificationType.ORDER_CONFIRMED,
            "Order Confirmed",
            f"Your order #{short_id} has been confirmed.",
            {"orderId": oid},
        )

    if etype == "order.packed":
        cid = p.get("customerId")
        if not cid:
            return None
        oid = str(p.get("orderId", ""))
        short_id = oid[:8] if oid else ""
        return (
            UUID(str(cid)),
            NotificationType.ORDER_PACKED,
            "Order Packed",
            f"Your order #{short_id} has been packed.",
            {"orderId": oid},
        )

    if etype == "order.ready_for_dispatch":
        cid = p.get("customerId")
        if not cid:
            return None
        oid = str(p.get("orderId", ""))
        short_id = oid[:8] if oid else ""
        return (
            UUID(str(cid)),
            NotificationType.ORDER_READY_FOR_DISPATCH,
            "Order Ready for Dispatch",
            f"Your order #{short_id} is ready for dispatch.",
            {"orderId": oid},
        )

    if etype == "order.pooled":
        cid = p.get("customerId")
        if not cid:
            return None
        oid = str(p.get("orderId", ""))
        short_id = oid[:8] if oid else ""
        return (
            UUID(str(cid)),
            NotificationType.ORDER_POOLED,
            "On a delivery route",
            f"Your order #{short_id} is on a delivery route.",
            {"orderId": oid},
        )

    if etype == "order.in_transit":
        cid = p.get("customerId")
        if not cid:
            return None
        oid = str(p.get("orderId", ""))
        short_id = oid[:8] if oid else ""
        return (
            UUID(str(cid)),
            NotificationType.ORDER_IN_TRANSIT,
            "Order Out for Delivery",
            f"Your order #{short_id} is out for delivery.",
            {"orderId": oid},
        )

    if etype in ("order.delivered", "delivery.completed"):
        cid = p.get("customerId")
        if not cid:
            return None
        oid = str(p.get("orderId", ""))
        short_id = oid[:8] if oid else ""
        return (
            UUID(str(cid)),
            NotificationType.ORDER_DELIVERED,
            "Order Delivered",
            f"Your order #{short_id} has been delivered successfully!",
            {"orderId": oid},
        )

    if etype == "order.completed":
        cid = p.get("customerId")
        if not cid:
            return None
        oid = str(p.get("orderId", ""))
        short_id = oid[:8] if oid else ""
        return (
            UUID(str(cid)),
            NotificationType.ORDER_COMPLETED,
            "Order Completed",
            f"Order #{short_id} is complete. Thank you!",
            {"orderId": oid},
        )

    if etype == "order.cancelled":
        cid = p.get("customerId")
        if not cid:
            return None
        oid = str(p.get("orderId", ""))
        short_id = oid[:8] if oid else ""
        return (
            UUID(str(cid)),
            NotificationType.ORDER_CANCELLED,
            "Order Cancelled",
            f"Order #{short_id} has been cancelled.",
            {"orderId": oid, "reason": p.get("reason")},
        )

    # 2. Payment events
    if etype == "payment.succeeded":
        cid = p.get("customerId")
        if not cid:
            return None
        amt = p.get("amount")
        amt_str = f"₹{amt:.2f}" if isinstance(amt, (int, float)) else "payment"
        return (
            UUID(str(cid)),
            NotificationType.PAYMENT_SUCCEEDED,
            "Payment Succeeded",
            f"Payment of {amt_str} confirmed.",
            {"paymentId": p.get("paymentId"), "amount": amt},
        )

    # 3. Reservation and Harvest events
    if etype == "reservation.accepted":
        cid = p.get("customerId")
        if not cid:
            return None
        deposit = p.get("deposit")
        dep_str = f"₹{deposit:.2f}" if isinstance(deposit, (int, float)) else "deposit"
        return (
            UUID(str(cid)),
            NotificationType.RESERVATION_ACCEPTED,
            "Plot Reservation Accepted",
            f"Your plot reservation was accepted! Please pay deposit of {dep_str}.",
            {"reservationId": p.get("reservationId"), "deposit": deposit},
        )

    if etype == "harvest.ready":
        cid = p.get("customerId")
        if not cid:
            return None
        crop = p.get("cropId", "crop")
        return (
            UUID(str(cid)),
            NotificationType.HARVEST_READY,
            "Crop Ready for Harvest",
            f"Your {crop} crop is ready for harvest inspection.",
            {"reservationId": p.get("reservationId"), "cropId": crop},
        )

    # 4. Quality graded
    if etype == "quality.graded":
        cid = p.get("customerId")
        if not cid:
            return None
        stype = p.get("subjectType")
        grade = p.get("grade", "")
        result = p.get("result", "")
        if stype == "ORDER":
            title = "Quality Inspection Passed" if result == "PASS" else "Quality Inspection Update"
            return (
                UUID(str(cid)),
                NotificationType.ORDER_QC_PASSED,
                title,
                f"Your order passed inspection with Grade {grade}."
                if result == "PASS"
                else f"Inspection result: Grade {grade}.",
                {"inspectionId": p.get("inspectionId"), "grade": grade, "result": result},
            )
        return (
            UUID(str(cid)),
            NotificationType.HARVEST_READY,
            "Harvest Inspection Graded",
            f"Harvest inspection completed with Grade {grade} ({result}).",
            {"inspectionId": p.get("inspectionId"), "grade": grade, "result": result},
        )

    # 5. Pooling assigned
    if etype == "pool.assigned":
        partner_id = p.get("partnerId")
        if partner_id:
            stops = p.get("stopCount", 0)
            km = p.get("totalKm", 0.0)
            km = km or 0.0
            day = p.get("poolDate")
            return (
                UUID(str(partner_id)),
                NotificationType.ORDER_POOLED,
                f"New route for {day}" if day else "Delivery Route Assigned",
                f"You have been assigned a route with {stops} stops ({km:.1f} km).",
                {"poolId": p.get("poolId"), "poolDate": day, "totalKm": km},
            )

    return None


def map_event_to_farmer_notification(
    event: Any,
) -> tuple[UUID, NotificationType, str, str, dict[str, Any]] | None:
    """The farmer's side of an order event, when the payload names the farm's owner (`farmOwnerId`).

    The customer notification for the same event comes from `map_event_to_notification`; one event can
    therefore produce two notifications, one for each party.
    """
    p = getattr(event, "payload", {}) if not isinstance(event, dict) else event.get("payload", {})
    etype = getattr(event, "type", "") if not isinstance(event, dict) else event.get("type", "")
    if etype == "listing.stock_low":
        return _stock_low_notification(p)
    owner = p.get("farmOwnerId")
    if not owner:
        return None
    oid = str(p.get("orderId", ""))
    short_id = oid[:8]
    total = p.get("total")
    total_str = f" (₹{total:.2f})" if isinstance(total, (int, float)) else ""
    if etype == "order.placed":
        return (
            UUID(str(owner)),
            NotificationType.ORDER_PLACED,
            "New order to pack",
            f"Order #{short_id}{total_str} is paid and waiting for you to pack it.",
            {"orderId": oid, "total": total},
        )
    if etype == "order.cancelled":
        return (
            UUID(str(owner)),
            NotificationType.ORDER_CANCELLED,
            "Order cancelled",
            f"Order #{short_id} was cancelled. There is nothing to pack.",
            {"orderId": oid},
        )
    if etype == "order.rated":
        score = p.get("score")
        comment = p.get("comment")
        said = f": \u201c{comment}\u201d" if comment else "."
        return (
            UUID(str(owner)),
            NotificationType.RATING_RECEIVED,
            f"New rating: {score} of 5",
            f"A customer rated order #{short_id} {score} of 5{said}",
            {"orderId": oid, "score": score},
        )
    if etype == "order.completed":
        return (
            UUID(str(owner)),
            NotificationType.ORDER_COMPLETED,
            "Order delivered",
            f"Order #{short_id} reached the customer. Your payout{total_str} is on its way.",
            {"orderId": oid, "total": total},
        )
    return None


def _stock_low_notification(
    p: dict[str, Any],
) -> tuple[UUID, NotificationType, str, str, dict[str, Any]] | None:
    farmer = p.get("farmerId")
    if not farmer:
        return None
    crop = (
        p.get("variety")
        and f"{p.get('cropName')} ({p['variety']})"
        or p.get("cropName", "A listing")
    )
    left = p.get("qtyAvailable", 0)
    unit = p.get("unit", "")
    data = {
        "listingId": str(p.get("listingId", "")),
        "qtyAvailable": left,
        "soldOut": bool(p.get("soldOut")),
    }
    if p.get("soldOut"):
        return (
            UUID(str(farmer)),
            NotificationType.LOW_STOCK,
            f"{crop} is sold out",
            f"{crop} has no stock left, so buyers no longer see it. Add stock to put it back on sale.",
            data,
        )
    return (
        UUID(str(farmer)),
        NotificationType.LOW_STOCK,
        f"Low stock: {crop}",
        f"Only {left:g} {unit} of {crop} left. Add stock before it sells out.",
        data,
    )


def make_event_handler(
    session_factory: async_sessionmaker[AsyncSession],
    settings: NotificationSettings,
):
    """Factory creating an asynchronous event handler for RabbitEventBus."""
    from farm_common.outbox import mark_processed

    fcm = settings.fcm_client()  # built once so its access token is cached

    async def handle_event(event: Any) -> None:
        mapped = [
            m
            for m in (map_event_to_notification(event), map_event_to_farmer_notification(event))
            if m is not None
        ]
        if not mapped:
            return

        event_id = getattr(event, "event_id", uuid4())

        pushes: list[tuple[list[str], str, str, str, dict[str, Any]]] = []
        async with session_factory() as session, session.begin():
            if not await mark_processed(session, ProcessedEvent, event_id):
                logger.info("Event %s already processed; skipping", event_id)
                return

            for target_user_id, notif_type, title, body, payload in mapped:
                session.add(
                    Notification(
                        id=uuid4(),
                        user_id=target_user_id,
                        type=notif_type.value,
                        title=title,
                        body=body,
                        payload=payload,
                        read_at=None,
                    )
                )
                # Device tokens for the push, read inside the same transaction
                result = await session.execute(
                    select(DeviceToken.token).where(DeviceToken.user_id == target_user_id)
                )
                pushes.append(
                    ([t for (t,) in result.all()], notif_type.value, title, body, payload)
                )

        # Push right away, outside the DB transaction. A push failure never loses the inbox row.
        if settings.push_enabled:
            dead: list[str] = []
            for tokens, type_name, title, body, payload in pushes:
                if not tokens:
                    continue
                try:
                    await send_push_notification(
                        tokens=tokens,
                        title=title,
                        body=body,
                        data=payload,
                        expo_push_url=settings.expo_push_url,
                        access_token=settings.expo_access_token,
                        fcm=fcm,
                        notification_type=type_name,
                        invalid_tokens=dead,
                    )
                except Exception:
                    logger.exception("Push dispatch failed for event %s", event_id)
            if dead:
                async with session_factory() as session, session.begin():
                    await session.execute(delete(DeviceToken).where(DeviceToken.token.in_(dead)))
                logger.info("Removed %d unregistered device token(s)", len(dead))

    return handle_event
