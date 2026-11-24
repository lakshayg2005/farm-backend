"""Photo processing as arq jobs (HLD section 8: "arq or Celery workers inside the owning service").

    arq media_service.worker.WorkerSettings          # run the worker (its own container)

For each photo (HLD section 12, "Upload a real photo"): read EXIF time and GPS, check the GPS
point is within 2 km of the farm or plot with ST_DWithin, write an EXIF-free public copy and a
thumbnail, then publish `media.processed` through the outbox.

A failing job is retried with a growing delay; after WORKER_MAX_ATTEMPTS the photo is FAILED.
Every minute a sweeper re-queues photos stuck in PROCESSING (e.g. Redis was down at /complete).
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from arq import Retry, cron, func
from arq.connections import RedisSettings
from geoalchemy2.elements import WKTElement
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.events import Event
from farm_common.log import configure_logging
from farm_common.outbox import add_event
from farm_common.telemetry import setup_telemetry, traced_job
from media_service.db import Media, OutboxEvent
from media_service.geo import GEO_KINDS, GeoResolver, check_location
from media_service.imaging import NotAnImage, process_image
from media_service.jobs import PROCESS_MEDIA, QUEUE_NAME, ArqQueue
from media_service.schemas import GeoStatus, MediaStatus, OwnerType
from media_service.settings import MediaSettings, get_media_settings
from media_service.storage import ObjectStore

logger = logging.getLogger(__name__)


def event_payload(media: Media) -> dict[str, Any]:
    return {
        "mediaId": str(media.id),
        "ownerService": media.owner_service,
        "ownerType": media.owner_type,
        "ownerId": str(media.owner_id),
        "uploadedBy": str(media.uploaded_by),
        "status": media.status,
        "url": media.url,
        "thumbUrl": media.thumb_url,
        "width": media.width,
        "height": media.height,
        "exifTime": media.exif_time.isoformat() if media.exif_time else None,
        "isGeoVerified": media.is_geo_verified,
        "geoStatus": media.geo_status,
        "distanceM": media.distance_m,
        "rejectReason": media.reject_reason,
    }


async def _process(
    session: AsyncSession,
    media: Media,
    store: ObjectStore,
    resolver: GeoResolver,
    settings: MediaSettings,
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

    exif = result.exif
    media.exif_time = exif.taken_at.astimezone(UTC) if exif.taken_at else None
    if exif.gps_lat is not None and exif.gps_lng is not None:
        media.taken_at = WKTElement(f"POINT({exif.gps_lng} {exif.gps_lat})", srid=4326)  # type: ignore[assignment]
    await session.flush()

    owner_type = OwnerType(media.owner_type)
    if owner_type in GEO_KINDS:
        reference = None
        if media.taken_at is not None:
            reference = await resolver.locate(owner_type, media.owner_id)
        geo_status, distance = await check_location(
            session, media, reference, settings.geo_verify_radius_m
        )
    else:
        geo_status, distance = GeoStatus.NOT_APPLICABLE, None

    public_key, thumb_key = f"public/{media.id}.jpg", f"thumbs/{media.id}.jpg"
    await store.put(public_key, result.public_jpeg, "image/jpeg")
    await store.put(thumb_key, result.thumb_jpeg, "image/jpeg")

    media.url, media.thumb_url = store.public_url(public_key), store.public_url(thumb_key)
    media.width, media.height = result.width, result.height
    media.geo_status, media.distance_m = geo_status, distance
    media.is_geo_verified = geo_status == GeoStatus.VERIFIED
    media.status = MediaStatus.READY


def _finish(session: AsyncSession, media: Media) -> None:
    media.processed_at = datetime.now(UTC)
    add_event(
        session,
        OutboxEvent,
        Event(type="media.processed", producer="media", payload=event_payload(media)),
    )


@traced_job()
async def process_media(ctx: dict[str, Any], media_id: str) -> str:
    """arq job: process one photo. Returns its final status (or SKIPPED)."""
    settings: MediaSettings = ctx["settings"]
    attempt: int = ctx.get("job_try", 1)
    async with ctx["session_factory"]() as session, session.begin():
        media = (
            await session.execute(
                select(Media)
                .where(Media.id == UUID(media_id), Media.status == MediaStatus.PROCESSING)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if media is None:
            return "SKIPPED"  # already done, deleted, or another worker has it
        try:
            async with session.begin_nested():
                await _process(session, media, ctx["store"], ctx["resolver"], settings)
        except Exception as exc:
            logger.exception("Processing media %s failed (attempt %d)", media_id, attempt)
            if attempt < settings.worker_max_attempts:
                raise Retry(defer=timedelta(seconds=5 * attempt)) from exc
            await session.refresh(media)
            media.status = MediaStatus.FAILED
            media.reject_reason = "PROCESSING_FAILED"
            media.geo_status = GeoStatus.NOT_APPLICABLE
        _finish(session, media)
        return media.status


async def requeue_stuck(ctx: dict[str, Any]) -> int:
    """Cron: queue photos left in PROCESSING (their job was never queued or got lost)."""
    settings: MediaSettings = ctx["settings"]
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.requeue_after_seconds)
    async with ctx["session_factory"]() as session:
        ids = (
            await session.execute(
                select(Media.id).where(
                    Media.status == MediaStatus.PROCESSING, Media.completed_at < cutoff
                )
            )
        ).scalars()
        stuck = list(ids)
    for media_id in stuck:
        await ctx["queue"].enqueue(media_id)
    return len(stuck)


async def startup(ctx: dict[str, Any]) -> None:
    from media_service.database import get_session_factory
    from media_service.geo import get_resolver
    from media_service.storage import get_store

    settings = get_media_settings()
    configure_logging("media-worker", settings.log_level)
    setup_telemetry("media-worker")
    ctx["settings"] = settings
    ctx["session_factory"] = get_session_factory()
    ctx["store"] = get_store()
    ctx["resolver"] = get_resolver()
    ctx["queue"] = ArqQueue(ctx["redis"])
    try:
        await ctx["store"].ensure_bucket()
    except Exception:  # storage may still be starting; jobs retry until it is up
        logger.exception("Could not reach object storage at %s", settings.s3_endpoint)


class WorkerSettings:
    functions = [
        func(process_media, name=PROCESS_MEDIA, max_tries=get_media_settings().worker_max_attempts)
    ]  # type: ignore[arg-type]
    cron_jobs = [cron(requeue_stuck, second=0, run_at_startup=True)]  # type: ignore[arg-type]
    on_startup = startup
    queue_name = QUEUE_NAME
    redis_settings = RedisSettings.from_dsn(get_media_settings().redis_url)
    keep_result = 300
