"""Public routes (behind the gateway at /api/v1/media).

Upload flow:
    1. POST /upload-url            -> { mediaId, uploadUrl, headers, expiresAt }
    2. PUT <uploadUrl> with the file body and exactly those headers (straight to storage)
    3. POST /{mediaId}/complete    -> 202, status PROCESSING
    4. the worker reads EXIF/GPS, makes the public copy + thumbnail -> READY, `media.processed`
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.auth import CurrentUser, Role, get_current_user
from farm_common.errors import AppError, Forbidden, NotFound
from media_service.database import get_session
from media_service.db import Media
from media_service.schemas import (
    EXTENSIONS,
    GeoStatus,
    MediaOut,
    MediaPage,
    MediaStatus,
    OwnerType,
    UploadUrlIn,
    UploadUrlOut,
)
from media_service.settings import MediaSettings, get_media_settings
from media_service.storage import ObjectStore, get_store

router = APIRouter(tags=["media"])


def _utc(value: datetime | None) -> datetime | None:
    # SQLite (tests) returns naive datetimes; Postgres returns aware ones. Both are UTC.
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def media_out(media: Media, store: ObjectStore) -> MediaOut:
    return MediaOut(
        id=media.id,
        owner_type=OwnerType(media.owner_type),
        owner_id=media.owner_id,
        uploaded_by=media.uploaded_by,
        status=MediaStatus(media.status),
        content_type=media.content_type,
        size_bytes=media.size_bytes,
        url=store.public_url(media.public_key) if media.public_key else None,
        thumb_url=store.public_url(media.thumb_key) if media.thumb_key else None,
        width=media.width,
        height=media.height,
        taken_at=_utc(media.taken_at),
        geo_status=GeoStatus(media.geo_status),
        is_geo_verified=media.geo_status == GeoStatus.VERIFIED,
        distance_m=media.distance_m,
        reject_reason=media.reject_reason,
        created_at=_utc(media.created_at),  # type: ignore[arg-type]
        processed_at=_utc(media.processed_at),
    )


async def _get(session: AsyncSession, media_id: UUID) -> Media:
    media = await session.get(Media, media_id)
    if media is None or media.status == MediaStatus.DELETED:
        raise NotFound("Photo not found", code="MEDIA_NOT_FOUND")
    return media


def _ensure_uploader(user: CurrentUser, media: Media) -> None:
    if str(media.uploaded_by) != user.id and not user.has_any(Role.ADMIN):
        raise Forbidden("This photo was uploaded by someone else", code="NOT_UPLOADER")


@router.post("/upload-url", status_code=status.HTTP_201_CREATED)
async def create_upload_url(
    body: UploadUrlIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    store: ObjectStore = Depends(get_store),
    settings: MediaSettings = Depends(get_media_settings),
) -> UploadUrlOut:
    """A signed URL to PUT one image (JPEG, PNG or WebP, at most 5 MB) for 5 minutes."""
    if body.size_bytes > settings.max_upload_bytes:
        raise AppError(
            "FILE_TOO_LARGE", f"Photos can be at most {settings.max_upload_bytes} bytes", 413
        )
    media_id = uuid4()
    key = f"originals/{media_id}.{EXTENSIONS[body.content_type]}"
    session.add(
        Media(
            id=media_id,
            owner_type=body.owner_type,
            owner_id=body.owner_id,
            uploaded_by=UUID(user.id),
            status=MediaStatus.PENDING_UPLOAD,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            file_name=body.file_name,
            original_key=key,
            geo_status=GeoStatus.PENDING,
        )
    )
    await session.commit()
    ttl = settings.upload_url_ttl_seconds
    return UploadUrlOut(
        media_id=media_id,
        upload_url=store.presign_put(key, body.content_type, body.size_bytes, ttl),
        headers={"Content-Type": body.content_type},
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
    )


@router.post("/{media_id}/complete", status_code=status.HTTP_202_ACCEPTED)
async def complete_upload(
    media_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    store: ObjectStore = Depends(get_store),
    settings: MediaSettings = Depends(get_media_settings),
) -> MediaOut:
    """Call after the PUT succeeded. Processing runs in the background; poll GET /{id} or wait
    for `media.processed`. Calling it again is harmless."""
    media = await _get(session, media_id)
    _ensure_uploader(user, media)
    if media.status != MediaStatus.PENDING_UPLOAD:
        return media_out(media, store)

    info = await store.head(media.original_key)
    if info is None:
        raise AppError("UPLOAD_MISSING", "The file has not been uploaded yet", 409)
    reason = None
    if info.size > settings.max_upload_bytes:
        reason = "FILE_TOO_LARGE"
    elif (info.content_type or "").split(";")[0].strip() != media.content_type:
        reason = "TYPE_MISMATCH"
    if reason:
        await store.delete(media.original_key)
        media.status, media.reject_reason = MediaStatus.REJECTED, reason
        media.geo_status = GeoStatus.NOT_APPLICABLE
        await session.commit()
        raise AppError(reason, "The uploaded file does not match the upload request", 422)

    media.size_bytes = info.size
    media.status = MediaStatus.PROCESSING
    media.completed_at = datetime.now(UTC)
    await session.commit()
    return media_out(media, store)


@router.get("/owners/{owner_type}/{owner_id}")
async def list_media(
    owner_type: OwnerType,
    owner_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
    store: ObjectStore = Depends(get_store),
) -> MediaPage:
    """READY photos of one farm / listing / plot / ..., newest first."""
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    rows = (
        (
            await session.execute(
                select(Media)
                .where(
                    Media.owner_type == owner_type,
                    Media.owner_id == owner_id,
                    Media.status == MediaStatus.READY,
                )
                .order_by(Media.created_at.desc(), Media.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    return MediaPage(
        items=[media_out(m, store) for m in rows[:limit]],
        next_cursor=str(offset + limit) if len(rows) > limit else None,
    )


@router.get("/{media_id}")
async def get_media(
    media_id: UUID,
    session: AsyncSession = Depends(get_session),
    store: ObjectStore = Depends(get_store),
) -> MediaOut:
    return media_out(await _get(session, media_id), store)


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    media_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    store: ObjectStore = Depends(get_store),
) -> Response:
    """Uploader (or admin) only. Removes the original and the public copies."""
    media = await _get(session, media_id)
    _ensure_uploader(user, media)
    keys = [k for k in (media.original_key, media.public_key, media.thumb_key) if k]
    media.status = MediaStatus.DELETED
    await session.commit()
    await store.delete(*keys)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
