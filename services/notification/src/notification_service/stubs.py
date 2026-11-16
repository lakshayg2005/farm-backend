"""Canned example data returned by contract stub routes for the notification service."""

from datetime import UTC, datetime
from uuid import UUID

from notification_service.schemas import (
    DeviceTokenIn,
    DeviceTokenOut,
    NotificationOut,
    NotificationPage,
    NotificationType,
    UnreadCountOut,
)

SAMPLE_USER_ID = UUID("7c1d9f0a-2b34-4e6f-8a90-1b2c3d4e5f60")
SAMPLE_DEVICE_ID = UUID("4d9b1c2e-7f3a-4e6f-8a90-2c3d4e5f6a01")
NOTIFICATION_ID_1 = UUID("1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d")
NOTIFICATION_ID_2 = UUID("2b3c4d5e-6f7a-8b9c-0d1e-2f3a4b5c6d7e")

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)

SAMPLE_NOTIFICATIONS = [
    NotificationOut(
        id=NOTIFICATION_ID_1,
        user_id=SAMPLE_USER_ID,
        type=NotificationType.ORDER_PLACED,
        title="Order Placed Successfully",
        body="Your produce order #a1f0c9d8 has been placed with Sunrise Organic Farm.",
        payload={"orderId": "a1f0c9d8-7e6b-4a5c-9d4e-3f2a1b0c9d8e", "total": 450.0},
        read_at=None,
        created_at=NOW,
    ),
    NotificationOut(
        id=NOTIFICATION_ID_2,
        user_id=SAMPLE_USER_ID,
        type=NotificationType.PAYMENT_SUCCEEDED,
        title="Payment Succeeded",
        body="Payment of ₹450.00 confirmed.",
        payload={"paymentId": "3e8a4b6c-9d12-4f7a-b1c3-5d6e7f8a9b01", "amount": 450.0},
        read_at=NOW,
        created_at=NOW,
    ),
]


def list_notifications(
    user_id: UUID,
    cursor: str | None = None,
    limit: int = 20,
    unread_only: bool = False,
) -> NotificationPage:
    items = [n for n in SAMPLE_NOTIFICATIONS if n.user_id == user_id]
    if unread_only:
        items = [n for n in items if n.read_at is None]
    return NotificationPage(
        items=items[:limit],
        unread_count=sum(1 for n in items if n.read_at is None),
        next_cursor=None,
    )


def register_device(user_id: UUID, payload: DeviceTokenIn) -> DeviceTokenOut:
    return DeviceTokenOut(
        id=SAMPLE_DEVICE_ID,
        user_id=user_id,
        token=payload.token,
        platform=payload.platform,
        created_at=NOW,
        updated_at=NOW,
    )


def mark_read(user_id: UUID, notification_id: UUID) -> NotificationOut:
    for n in SAMPLE_NOTIFICATIONS:
        if n.id == notification_id:
            return NotificationOut(
                id=n.id,
                user_id=user_id,
                type=n.type,
                title=n.title,
                body=n.body,
                payload=n.payload,
                read_at=NOW,
                created_at=n.created_at,
            )
    return NotificationOut(
        id=notification_id,
        user_id=user_id,
        type=NotificationType.SYSTEM,
        title="Notification",
        body="Notification marked as read.",
        payload={},
        read_at=NOW,
        created_at=NOW,
    )


def unread_count(user_id: UUID) -> UnreadCountOut:
    return UnreadCountOut(
        count=sum(1 for n in SAMPLE_NOTIFICATIONS if n.user_id == user_id and n.read_at is None)
    )
