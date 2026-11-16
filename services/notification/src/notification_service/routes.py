"""HTTP routes of the notification service. Request and response models are in schemas.py (the contract)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from farm_common.auth import CurrentUser, get_current_user
from notification_service import stubs
from notification_service.schemas import (
    DeviceTokenIn,
    DeviceTokenOut,
    NotificationOut,
    NotificationPage,
    UnreadCountOut,
)

router = APIRouter(tags=["notifications"])


@router.get(
    "/notifications",
    response_model=NotificationPage,
    summary="List in-app notifications for the current user",
)
async def get_notifications(
    cursor: str | None = Query(None, description="Cursor for pagination"),
    limit: int = Query(20, ge=1, le=100, description="Page size"),
    unread_only: bool = Query(
        False, alias="unreadOnly", description="Filter to only unread notifications"
    ),
    user: CurrentUser = Depends(get_current_user),
) -> NotificationPage:
    return stubs.list_notifications(
        user_id=UUID(user.id),
        cursor=cursor,
        limit=limit,
        unread_only=unread_only,
    )


@router.get(
    "/notifications/unread-count",
    response_model=UnreadCountOut,
    summary="Get unread notification count for the current user",
)
async def get_unread_count(
    user: CurrentUser = Depends(get_current_user),
) -> UnreadCountOut:
    return stubs.unread_count(user_id=UUID(user.id))


@router.post(
    "/notifications/devices",
    response_model=DeviceTokenOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a device token for push notifications",
)
async def register_device_token(
    payload: DeviceTokenIn,
    user: CurrentUser = Depends(get_current_user),
) -> DeviceTokenOut:
    return stubs.register_device(user_id=UUID(user.id), payload=payload)


@router.delete(
    "/notifications/devices/{token}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister a device token",
)
async def unregister_device_token(
    token: str,
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationOut,
    summary="Mark a notification as read",
)
async def mark_notification_read(
    notification_id: UUID,
    user: CurrentUser = Depends(get_current_user),
) -> NotificationOut:
    return stubs.mark_read(user_id=UUID(user.id), notification_id=notification_id)


@router.post(
    "/notifications/read-all",
    response_model=UnreadCountOut,
    summary="Mark all notifications as read for current user",
)
async def mark_all_read(
    user: CurrentUser = Depends(get_current_user),
) -> UnreadCountOut:
    return UnreadCountOut(count=0)
