"""Internal routes: reachable only inside the Docker network, never through the gateway."""

import json
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.errors import NotFound
from media_service.database import get_session
from media_service.db import Media
from media_service.routes import media_out
from media_service.schemas import GeoPoint, MediaInternalOut, MediaStatus
from media_service.storage import ObjectStore, get_store

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/media/{media_id}")
async def get_media_internal(
    media_id: UUID,
    session: AsyncSession = Depends(get_session),
    store: ObjectStore = Depends(get_store),
) -> MediaInternalOut:
    """Full details incl. where the photo was taken, e.g. for quality or logistics."""
    media = await session.get(Media, media_id)
    if media is None:
        raise NotFound("Photo not found", code="MEDIA_NOT_FOUND")
    taken_at = None
    if media.taken_at is not None:
        geojson = await session.scalar(
            select(func.ST_AsGeoJSON(Media.taken_at)).where(Media.id == media.id)
        )
        taken_at = GeoPoint.model_validate(json.loads(str(geojson)))
    return MediaInternalOut(
        **media_out(media).model_dump(),
        taken_at=taken_at,
        original_url=(
            store.presign_get(media.original_key, 300)
            if media.status == MediaStatus.READY
            else None
        ),
    )
