import io
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from arq import Retry
from PIL import ExifTags, Image
from sqlalchemy import update

from media_service.db import Media
from media_service.geo import HttpGeoResolver
from media_service.imaging import read_exif
from media_service.schemas import OwnerType
from media_service.settings import MediaSettings
from media_service.worker import process_media, requeue_stuck

KM = 0.009  # degrees of latitude per km (roughly)
FARM = (21.19, 81.28)


def _jpeg_with(gps: dict | None = None, details: dict | None = None) -> bytes:
    image = Image.new("RGB", (8, 8))
    exif = Image.Exif()
    if gps:
        exif.get_ifd(ExifTags.IFD.GPSInfo).update(gps)
    if details:
        exif.get_ifd(ExifTags.IFD.Exif).update(details)
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_exifread_south_west():
    data = _jpeg_with(
        {
            ExifTags.GPS.GPSLatitudeRef: "S",
            ExifTags.GPS.GPSLatitude: (33.0, 52.0, 4.0),
            ExifTags.GPS.GPSLongitudeRef: "W",
            ExifTags.GPS.GPSLongitude: (70.0, 30.0, 0.0),
        }
    )
    exif = read_exif(data, "Asia/Kolkata")
    assert exif.gps_lat == pytest.approx(-33.8678, abs=1e-4)
    assert exif.gps_lng == pytest.approx(-70.5, abs=1e-4)


def test_exifread_no_fix_and_no_exif():
    zero = _jpeg_with(
        {
            ExifTags.GPS.GPSLatitudeRef: "N",
            ExifTags.GPS.GPSLatitude: (0.0, 0.0, 0.0),
            ExifTags.GPS.GPSLongitudeRef: "E",
            ExifTags.GPS.GPSLongitude: (0.0, 0.0, 0.0),
        }
    )
    assert read_exif(zero, "Asia/Kolkata").gps_lat is None
    empty = read_exif(_jpeg_with(), "Asia/Kolkata")
    assert (empty.gps_lat, empty.taken_at) == (None, None)
    assert read_exif(b"not an image", "Asia/Kolkata").taken_at is None


def test_exif_time_uses_offset_when_present():
    data = _jpeg_with(
        details={
            ExifTags.Base.DateTimeOriginal: "2026:11:01 10:00:00",
            ExifTags.Base.OffsetTimeOriginal: "+02:00",
        }
    )
    taken = read_exif(data, "Asia/Kolkata").taken_at
    assert taken is not None and taken.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize(
    "north_km, status, low, high",
    [
        (0.3, "VERIFIED", 0, 0),  # inside the boundary
        (2.2, "VERIFIED", 1400, 1800),  # ~1.65 km past the edge
        (3.0, "TOO_FAR", 2300, 2600),  # ~2.45 km past the edge
        (10.0, "TOO_FAR", 9300, 9600),  # ~9.45 km past the edge
    ],
)
async def test_st_dwithin_against_the_farm_boundary(
    uploader, make_photo, north_km, status, low, high
):
    media = await uploader.upload(make_photo(gps=(FARM[0] + north_km * KM, FARM[1])))

    assert media["geoStatus"] == status
    assert media["isGeoVerified"] is (status == "VERIFIED")
    assert low <= media["distanceM"] <= high


async def test_point_only_reference(uploader, make_photo, resolver):
    listing = uuid4()
    resolver.places[listing] = {"point": {"type": "Point", "coordinates": [81.28, 21.19]}}
    media = await uploader.upload(
        make_photo(gps=(FARM[0] + 1.5 * KM, FARM[1])), ownerType="LISTING", ownerId=str(listing)
    )
    assert media["geoStatus"] == "VERIFIED"
    assert 1400 < media["distanceM"] < 1600


async def test_http_resolver(monkeypatch):
    settings = MediaSettings(catalog_internal_url="http://catalog", land_internal_url="http://land")
    missing = UUID(int=0)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith(str(missing)):
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        return httpx.Response(
            200, json={"point": {"type": "Point", "coordinates": [81.28, 21.19]}, "boundary": None}
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    resolver = HttpGeoResolver(settings)

    plot = uuid4()
    ref = await resolver.locate(OwnerType.PLOT, plot)
    assert ref is not None and ref["point"]["coordinates"] == [81.28, 21.19]
    assert seen[-1] == f"http://land/internal/geo/plot/{plot}"
    assert await resolver.locate(OwnerType.FARM, missing) is None
    assert seen[-1] == f"http://catalog/internal/geo/farm/{missing}"
    assert await resolver.locate(OwnerType.USER, uuid4()) is None


async def test_job_retries_then_fails(uploader, make_photo, store, outbox):
    data = make_photo(gps=None)
    ticket = await uploader.request(data)
    await uploader.put(ticket, data)
    await uploader.complete(ticket["mediaId"])
    assert uploader.queue.jobs == [UUID(ticket["mediaId"])]

    async def broken(key: str, data: bytes, content_type: str) -> None:
        raise ConnectionError("storage down")

    store.put = broken  # type: ignore[method-assign]
    for attempt in range(1, uploader.settings.worker_max_attempts):
        with pytest.raises(Retry):
            await process_media(uploader.ctx(job_try=attempt), ticket["mediaId"])
    last = uploader.settings.worker_max_attempts
    assert await process_media(uploader.ctx(job_try=last), ticket["mediaId"]) == "FAILED"

    media = (await uploader.client.get(f"/{ticket['mediaId']}")).json()
    assert media["status"] == "FAILED"
    assert [e.payload["status"] for e in await outbox()] == ["FAILED"]


async def test_job_for_a_finished_photo_is_skipped(uploader, make_photo):
    media = await uploader.upload(make_photo(gps=None))
    assert await process_media(uploader.ctx(), media["id"]) == "SKIPPED"


async def test_sweeper_requeues_stuck_photos(uploader, make_photo, session_factory):
    data = make_photo(gps=None)
    ticket = await uploader.request(data)
    await uploader.put(ticket, data)
    await uploader.complete(ticket["mediaId"])
    uploader.queue.jobs.clear()  # pretend Redis lost the job

    assert await requeue_stuck(uploader.ctx()) == 0  # not old enough yet
    async with session_factory() as session:
        await session.execute(
            update(Media).values(completed_at=datetime.now(UTC) - timedelta(minutes=10))
        )
        await session.commit()
    assert await requeue_stuck(uploader.ctx()) == 1
    assert await uploader.process()
    assert (await uploader.client.get(f"/{ticket['mediaId']}")).json()["status"] == "READY"
