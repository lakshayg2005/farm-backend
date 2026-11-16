from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field

from farm_common.schemas import ApiModel


class NotificationType(StrEnum):
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_CONFIRMED = "ORDER_CONFIRMED"
    ORDER_PACKED = "ORDER_PACKED"
    ORDER_QC_PASSED = "ORDER_QC_PASSED"
    ORDER_READY_FOR_DISPATCH = "ORDER_READY_FOR_DISPATCH"
    ORDER_POOLED = "ORDER_POOLED"
    ORDER_IN_TRANSIT = "ORDER_IN_TRANSIT"
    ORDER_DELIVERED = "ORDER_DELIVERED"
    ORDER_COMPLETED = "ORDER_COMPLETED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    PAYMENT_SUCCEEDED = "PAYMENT_SUCCEEDED"
    RESERVATION_ACCEPTED = "RESERVATION_ACCEPTED"
    HARVEST_READY = "HARVEST_READY"
    SYSTEM = "SYSTEM"


class DevicePlatform(StrEnum):
    IOS = "ios"
    ANDROID = "android"
    WEB = "web"


class NotificationOut(ApiModel):
    id: UUID
    user_id: UUID
    type: NotificationType
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=1000)
    payload: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime


class NotificationPage(ApiModel):
    items: list[NotificationOut]
    unread_count: int = Field(ge=0)
    next_cursor: str | None = None


class DeviceTokenIn(ApiModel):
    token: str = Field(min_length=10, max_length=256, description="Expo push token or FCM token")
    platform: DevicePlatform


class DeviceTokenOut(ApiModel):
    id: UUID
    user_id: UUID
    token: str
    platform: DevicePlatform
    created_at: datetime
    updated_at: datetime


class UnreadCountOut(ApiModel):
    count: int = Field(ge=0)
