"""Background worker: turns PROCESSING uploads into READY photos.

For each photo: read EXIF time and GPS, check the GPS point is within 2 km of the farm or plot,
write an EXIF-free public copy and a thumbnail, then publish `media.processed` (outbox).

Runs inside the media service by default (WORKER_ENABLED=true), or on its own:

    uv run --package farm-media python -m media_service.worker

Rows are claimed with FOR UPDATE SKIP LOCKED, so several workers can run side by side.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farm_common.events import Event
from farm_common.outbox import add_event
from media_service.db import Media, OutboxEvent
from media_service.geo import GEO_SOURCES, GeoResolver, classify
from media_service.imaging import NotAnImage, process_image
from media_service.schemas import GeoStatus, MediaStatus, OwnerType
from media_service.settings import MediaSettings
from media_service.storage import ObjectStore

logger = logging.getLogger(__name__)


def public_payload(media: Media, store: ObjectStore) -> dict[str, Any]:
    return {
        "mediaId": str(media.id),
        "ownerType": media.owner_type,
        "ownerId": str(media.owner_id),
        "uploadedBy": str(media.uploaded_by),
        "status": media.status,
        "url": store.public_url(media.public_key) if media.public_key else None,
        "thumbUrl": store.public_url(media.thumb_key) if media.thumb_key else None,
        "width": media.width,
        "height": media.height,
        "takenAt": media.taken_at.isoformat() if media.taken_at else None,
        "geoStatus": media.geo_status,
        "isGeoVerified": media.geo_status == GeoStatus.VERIFIED,
        "distanceM": media.distance_m,
        "rejectReason": media.reject_reason,
    }


async def _process(
    media: Media, store: ObjectStore, resolver: GeoResolver, settings: MediaSettings
) -> None:
    original = await store.get(media.original_key)
    try:
        result = await asyncio.to_thread(
            process_image,
            original,
            media.content_type,
            public_max_px=settings.public_max_px,
            thumb_max_px=settings.thumb_max_px,
            default_tz=settings.exif_default_tz,
        )
    except NotAnImage as exc:
        media.status = MediaStatus.REJECTED
        media.reject_reason = str(exc)
        media.geo_status = GeoStatus.NOT_APPLICABLE
        await store.delete(media.original_key)
        return

    owner_type = OwnerType(media.owner_type)
    if owner_type in GEO_SOURCES:
        reference = None
        if result.gps_lat is not None:
            reference = await resolver.locate(owner_type, media.owner_id)
        geo_status, distance = classify(
            result.gps_lat, result.gps_lng, reference, settings.geo_verify_radius_m
        )
    else:
        geo_status, distance = GeoStatus.NOT_APPLICABLE, None

    public_key, thumb_key = f"public/{media.id}.jpg", f"thumbs/{media.id}.jpg"
    await store.put(public_key, result.public_jpeg, "image/jpeg")
    await store.put(thumb_key, result.thumb_jpeg, "image/jpeg")

    media.public_key, media.thumb_key = public_key, thumb_key
    media.width, media.height = result.width, result.height
    media.taken_at = result.taken_at.astimezone(UTC) if result.taken_at else None
    media.gps_lat, media.gps_lng = result.gps_lat, result.gps_lng
    media.geo_status, media.distance_m = geo_status, distance
    media.status = MediaStatus.READY


async def process_one(
    session_factory: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    resolver: GeoResolver,
    settings: MediaSettings,
) -> bool:
    """Process the oldest waiting photo. Returns False when there was nothing to do."""
    async with session_factory() as session, session.begin():
        media = (
            await session.execute(
                select(Media)
                .where(
                    Media.status == MediaStatus.PROCESSING,
                    Media.attempts < settings.worker_max_attempts,
                )
                .order_by(Media.completed_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if media is None:
            return False
        media.attempts += 1
        try:
            async with session.begin_nested():
                await _process(media, store, resolver, settings)
        except Exception:
            logger.exception("Processing media %s failed (attempt %d)", media.id, media.attempts)
            await session.refresh(media)
            if media.attempts >= settings.worker_max_attempts:
                media.status = MediaStatus.FAILED
                media.reject_reason = "PROCESSING_FAILED"
            else:
                return True
        media.processed_at = datetime.now(UTC)
        add_event(
            session,
            OutboxEvent,
            Event(type="media.processed", producer="media", payload=public_payload(media, store)),
        )
        return True


async def run_worker(
    session_factory: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    resolver: GeoResolver,
    settings: MediaSettings,
) -> None:
    while True:
        try:
            busy = await process_one(session_factory, store, resolver, settings)
        except Exception:
            logger.exception("Media worker loop failed; will retry")
            busy = False
        if not busy:
            await asyncio.sleep(settings.worker_poll_seconds)


def main() -> None:
    from farm_common.log import configure_logging
    from media_service.database import get_session_factory
    from media_service.geo import get_resolver
    from media_service.settings import get_media_settings
    from media_service.storage import get_store

    settings = get_media_settings()
    configure_logging("media-worker", settings.log_level)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker(get_session_factory(), get_store(), get_resolver(), settings))


if __name__ == "__main__":
    main()
