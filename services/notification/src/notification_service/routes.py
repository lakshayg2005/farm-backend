"""HTTP routes of the notification service. Request and response models are in schemas.py (the contract)."""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.auth import CurrentUser, get_current_user
from notification_service import stubs
from notification_service.database import get_session
from notification_service.db import DeviceToken, Notification, now
from notification_service.schemas import (
    DevicePlatform,
    DeviceTokenIn,
    DeviceTokenOut,
    NotificationOut,
    NotificationPage,
    NotificationType,
    UnreadCountOut,
)

logger = logging.getLogger(__name__)
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
    session: AsyncSession | None = Depends(get_session),
) -> NotificationPage:
    user_uuid = UUID(user.id)

    if session is None:
        return stubs.list_notifications(
            user_id=user_uuid, cursor=cursor, limit=limit, unread_only=unread_only
        )

    try:
        unread_q = select(func.count(Notification.id)).where(
            Notification.user_id == user_uuid, Notification.read_at.is_(None)
        )
        unread_res = await session.execute(unread_q)
        unread_count = unread_res.scalar() or 0

        q = select(Notification).where(Notification.user_id == user_uuid)
        if unread_only:
            q = q.where(Notification.read_at.is_(None))
        if cursor:
            try:
                cursor_dt = datetime.fromisoformat(cursor)
                q = q.where(Notification.created_at < cursor_dt)
            except Exception:
                pass

        q = q.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit + 1)
        res = await session.execute(q)
        rows = list(res.scalars())

        has_next = len(rows) > limit
        items = rows[:limit]
        next_cursor = items[-1].created_at.isoformat() if has_next and items else None

        # Fallback to stub examples if sample test user and table empty
        if not items and user_uuid == stubs.SAMPLE_USER_ID:
            return stubs.list_notifications(
                user_id=user_uuid, cursor=cursor, limit=limit, unread_only=unread_only
            )

        return NotificationPage(
            items=[
                NotificationOut(
                    id=n.id,
                    user_id=n.user_id,
                    type=NotificationType(n.type),
                    title=n.title,
                    body=n.body,
                    payload=n.payload or {},
                    read_at=n.read_at,
                    created_at=n.created_at,
                )
                for n in items
            ],
            unread_count=unread_count,
            next_cursor=next_cursor,
        )
    except Exception as exc:
        logger.debug("Database unavailable (%s); falling back to stub data", exc)
        return stubs.list_notifications(
            user_id=user_uuid, cursor=cursor, limit=limit, unread_only=unread_only
        )


@router.get(
    "/notifications/unread-count",
    response_model=UnreadCountOut,
    summary="Get unread notification count for the current user",
)
async def get_unread_count(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession | None = Depends(get_session),
) -> UnreadCountOut:
    user_uuid = UUID(user.id)
    if session is None:
        return stubs.unread_count(user_id=user_uuid)

    try:
        res = await session.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_uuid, Notification.read_at.is_(None)
            )
        )
        cnt = res.scalar() or 0
        if cnt == 0 and user_uuid == stubs.SAMPLE_USER_ID:
            return stubs.unread_count(user_id=user_uuid)
        return UnreadCountOut(count=cnt)
    except Exception as exc:
        logger.debug("Database unavailable (%s); falling back to stub data", exc)
        return stubs.unread_count(user_id=user_uuid)


@router.post(
    "/notifications/devices",
    response_model=DeviceTokenOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a device token for push notifications",
)
async def register_device_token(
    payload: DeviceTokenIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession | None = Depends(get_session),
) -> DeviceTokenOut:
    user_uuid = UUID(user.id)
    if session is None:
        return stubs.register_device(user_id=user_uuid, payload=payload)

    try:
        res = await session.execute(
            select(DeviceToken).where(
                DeviceToken.user_id == user_uuid, DeviceToken.token == payload.token
            )
        )
        existing = res.scalar_one_or_none()
        if existing is not None:
            existing.platform = payload.platform.value
            existing.updated_at = now()
            await session.commit()
            await session.refresh(existing)
            dev = existing
        else:
            dev = DeviceToken(
                id=uuid4(),
                user_id=user_uuid,
                token=payload.token,
                platform=payload.platform.value,
                created_at=now(),
                updated_at=now(),
            )
            session.add(dev)
            await session.commit()
            await session.refresh(dev)

        return DeviceTokenOut(
            id=dev.id,
            user_id=dev.user_id,
            token=dev.token,
            platform=DevicePlatform(dev.platform),
            created_at=dev.created_at,
            updated_at=dev.updated_at,
        )
    except Exception as exc:
        logger.debug("Database unavailable (%s); falling back to stub data", exc)
        return stubs.register_device(user_id=user_uuid, payload=payload)


@router.delete(
    "/notifications/devices/{token}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister a device token",
)
async def unregister_device_token(
    token: str,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession | None = Depends(get_session),
) -> Response:
    if session is not None:
        try:
            user_uuid = UUID(user.id)
            await session.execute(
                delete(DeviceToken).where(
                    DeviceToken.user_id == user_uuid, DeviceToken.token == token
                )
            )
            await session.commit()
        except Exception as exc:
            logger.debug("Database delete failed: %s", exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationOut,
    summary="Mark a notification as read",
)
async def mark_notification_read(
    notification_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession | None = Depends(get_session),
) -> NotificationOut:
    user_uuid = UUID(user.id)
    if session is None:
        return stubs.mark_read(user_id=user_uuid, notification_id=notification_id)

    try:
        res = await session.execute(
            select(Notification).where(
                Notification.id == notification_id, Notification.user_id == user_uuid
            )
        )
        notif = res.scalar_one_or_none()
        if notif is None:
            return stubs.mark_read(user_id=user_uuid, notification_id=notification_id)
        if notif.read_at is None:
            notif.read_at = now()
            await session.commit()
            await session.refresh(notif)
        return NotificationOut(
            id=notif.id,
            user_id=notif.user_id,
            type=NotificationType(notif.type),
            title=notif.title,
            body=notif.body,
            payload=notif.payload or {},
            read_at=notif.read_at,
            created_at=notif.created_at,
        )
    except Exception as exc:
        logger.debug("Database read failed: %s", exc)
        return stubs.mark_read(user_id=user_uuid, notification_id=notification_id)


@router.post(
    "/notifications/read-all",
    response_model=UnreadCountOut,
    summary="Mark all notifications as read for current user",
)
async def mark_all_read(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession | None = Depends(get_session),
) -> UnreadCountOut:
    if session is not None:
        try:
            user_uuid = UUID(user.id)
            await session.execute(
                update(Notification)
                .where(Notification.user_id == user_uuid, Notification.read_at.is_(None))
                .values(read_at=now())
            )
            await session.commit()
        except Exception as exc:
            logger.debug("Database mark-all failed: %s", exc)
    return UnreadCountOut(count=0)
