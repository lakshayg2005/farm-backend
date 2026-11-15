import io
import math
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from PIL import ExifTags, Image
from shapely.geometry import Point, box

from media_service.geo import GeoReference, HttpGeoResolver, classify, distance_m
from media_service.imaging import read_gps, read_taken_at
from media_service.schemas import GeoStatus, OwnerType
from media_service.settings import MediaSettings


def haversine(lat1, lng1, lat2, lng2):
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    return (
        2
        * r
        * math.asin(
            math.sqrt(math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
        )
    )


@pytest.mark.parametrize("d_lat, d_lng", [(0.018, 0), (0, 0.019), (0.012, 0.013), (-0.02, -0.004)])
def test_point_distance_matches_haversine(d_lat, d_lng):
    ref = GeoReference(point=Point(81.28, 21.19))
    expected = haversine(21.19, 81.28, 21.19 + d_lat, 81.28 + d_lng)
    assert abs(distance_m(21.19 + d_lat, 81.28 + d_lng, ref) - expected) / expected < 0.005


def test_distance_to_boundary_is_zero_inside():
    ref = GeoReference(point=Point(81.28, 21.19), boundary=box(81.27, 21.18, 81.29, 21.2))
    assert distance_m(21.195, 81.285, ref) == 0
    # 0.01 deg of latitude north of the top edge ~ 1.1 km
    assert 1050 < distance_m(21.21, 81.28, ref) < 1150


def test_classify():
    ref = GeoReference(point=Point(81.28, 21.19))
    assert classify(None, None, ref, 2000) == (GeoStatus.NO_GPS, None)
    assert classify(21.19, 81.28, None, 2000) == (GeoStatus.NO_REFERENCE, None)
    assert classify(21.2, 81.28, ref, 2000)[0] == GeoStatus.VERIFIED
    assert classify(21.22, 81.28, ref, 2000)[0] == GeoStatus.TOO_FAR


def _exif(gps: dict | None = None) -> Image.Exif:
    image = Image.new("RGB", (4, 4))
    exif = Image.Exif()
    if gps:
        exif.get_ifd(ExifTags.IFD.GPSInfo).update(gps)
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif)
    return Image.open(io.BytesIO(out.getvalue())).getexif()


def test_gps_south_west_and_missing_fix():
    exif = _exif(
        {
            ExifTags.GPS.GPSLatitudeRef: "S",
            ExifTags.GPS.GPSLatitude: (33.0, 52.0, 4.0),
            ExifTags.GPS.GPSLongitudeRef: "W",
            ExifTags.GPS.GPSLongitude: (70.0, 30.0, 0.0),
        }
    )
    lat, lng = read_gps(exif)
    assert lat == pytest.approx(-33.8678, abs=1e-4)
    assert lng == pytest.approx(-70.5, abs=1e-4)

    zero = _exif(
        {
            ExifTags.GPS.GPSLatitudeRef: "N",
            ExifTags.GPS.GPSLatitude: (0.0, 0.0, 0.0),
            ExifTags.GPS.GPSLongitudeRef: "E",
            ExifTags.GPS.GPSLongitude: (0.0, 0.0, 0.0),
        }
    )
    assert read_gps(zero) == (None, None)
    assert read_gps(_exif()) == (None, None)


def test_exif_time_uses_offset_when_present():
    exif = _exif()
    exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = "2026:11:01 10:00:00"
    exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.OffsetTimeOriginal] = "+02:00"
    taken = read_taken_at(exif, "Asia/Kolkata")
    assert taken is not None and taken.utcoffset() == timedelta(hours=2)
    assert read_taken_at(_exif(), "Asia/Kolkata") is None


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
    assert ref is not None and ref.point.x == 81.28
    assert seen[-1] == f"http://land/internal/geo/plot/{plot}"
    assert await resolver.locate(OwnerType.FARM, missing) is None
    assert seen[-1] == f"http://catalog/internal/geo/farm/{missing}"
    assert await resolver.locate(OwnerType.USER, uuid4()) is None


async def test_worker_gives_up_after_max_attempts(uploader, make_photo, store, outbox):
    data = make_photo(gps=None)
    ticket = await uploader.request(data)
    await uploader.put(ticket, data)
    await uploader.complete(ticket["mediaId"])

    async def broken(key: str, data: bytes, content_type: str) -> None:
        raise ConnectionError("storage down")

    store.put = broken  # type: ignore[method-assign]
    for _ in range(uploader.settings.worker_max_attempts):
        assert await uploader.process()
    assert not await uploader.process()

    media = (await uploader.client.get(f"/{ticket['mediaId']}")).json()
    assert media["status"] == "FAILED"
    events = await outbox()
    assert [e.payload["status"] for e in events] == ["FAILED"]
