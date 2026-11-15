"""Internal routes: reachable only inside the Docker network, never through the gateway."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.errors import NotFound
from media_service.database import get_session
from media_service.db import Media
from media_service.routes import media_out
from media_service.schemas import MediaInternalOut
from media_service.storage import ObjectStore, get_store

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/media/{media_id}")
async def get_media_internal(
    media_id: UUID,
    session: AsyncSession = Depends(get_session),
    store: ObjectStore = Depends(get_store),
) -> MediaInternalOut:
    """Full details incl. GPS, e.g. for quality or logistics to check where a photo was taken."""
    media = await session.get(Media, media_id)
    if media is None:
        raise NotFound("Photo not found", code="MEDIA_NOT_FOUND")
    return MediaInternalOut(
        **media_out(media, store).model_dump(),
        gps_lat=media.gps_lat,
        gps_lng=media.gps_lng,
        original_url=(
            store.presign_get(media.original_key, 300) if media.status == "READY" else None
        ),
    )
