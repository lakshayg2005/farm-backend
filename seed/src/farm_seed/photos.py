"""Demo photos: upload the committed JPEGs through the real media flow, with EXIF GPS and capture time
written to the file first, so the media service's location check runs for real (verified, or too far).

The photos in seed/assets/photos are openly licensed (see seed/assets/CREDITS.md). Their original EXIF is
replaced: a demo photo claims it was taken where the demo farm is, and that is documented in docs/DEMO.md."""

import io
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from PIL import Image

from farm_seed.client import Gateway, SeedError

ASSETS = Path(__file__).resolve().parents[2] / "assets" / "photos"


def _dms(value: float) -> tuple[float, float, float]:
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    return float(degrees), float(minutes), round((value - degrees - minutes / 60) * 3600, 4)


def with_exif(key: str, lat: float, lng: float, taken: datetime) -> bytes:
    """The photo as JPEG bytes carrying GPS (lat, lng) and a capture time."""
    image = Image.open(ASSETS / f"{key}.jpg").convert("RGB")
    exif = image.getexif()
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = ("N" if lat >= 0 else "S"), _dms(lat)
    gps[3], gps[4] = ("E" if lng >= 0 else "W"), _dms(lng)
    exif.get_ifd(0x8769)[36867] = taken.strftime("%Y:%m:%d %H:%M:%S")  # DateTimeOriginal
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85, exif=exif)
    return out.getvalue()


def upload(
    gw: Gateway,
    token: str,
    key: str,
    owner_type: str,
    owner_id: str,
    lat: float,
    lng: float,
    days_ago: float = 0,
    wait: float = 60,
) -> str:
    """Upload one photo and wait until the media service has processed it. Returns the media id."""
    taken = datetime.now(UTC).replace(microsecond=0) - timedelta(days=days_ago)
    data = with_exif(key, lat, lng, taken)
    signed = gw.call(
        "POST",
        "/media/upload-url",
        token=token,
        json={
            "contentType": "image/jpeg",
            "ownerType": owner_type,
            "ownerId": owner_id,
            "sizeBytes": len(data),
            "fileName": f"{key}.jpg",
        },
    ).json()
    put = httpx.put(signed["uploadUrl"], content=data, headers=signed["headers"], timeout=60)
    if put.status_code >= 300:
        raise SeedError(f"photo upload to storage failed: {put.status_code} {put.text[:200]}")
    gw.call("POST", f"/media/{signed['mediaId']}/complete", token=token)
    deadline = time.monotonic() + wait
    while True:
        media = gw.call("GET", f"/media/{signed['mediaId']}", token=token).json()
        if media["status"] == "READY":
            return signed["mediaId"]
        if media["status"] in ("REJECTED", "DELETED") or time.monotonic() > deadline:
            raise SeedError(f"photo {key} did not become ready: {media['status']}")
        time.sleep(0.5)
