"""Reading EXIF (capture time, GPS) and making the public copy and thumbnail with Pillow."""

import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

# Refuse decompression bombs (a tiny file that expands to a gigantic bitmap).
Image.MAX_IMAGE_PIXELS = 50_000_000
ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class NotAnImage(ValueError):
    pass


@dataclass
class Processed:
    width: int
    height: int
    taken_at: datetime | None
    gps_lat: float | None
    gps_lng: float | None
    public_jpeg: bytes
    thumb_jpeg: bytes


def _degrees(value: Any) -> float | None:
    try:
        d, m, s = (float(v) for v in value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return d + m / 60 + s / 3600


def read_gps(exif: Image.Exif) -> tuple[float | None, float | None]:
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    if not gps:
        return None, None
    lat = _degrees(gps.get(ExifTags.GPS.GPSLatitude))
    lng = _degrees(gps.get(ExifTags.GPS.GPSLongitude))
    if lat is None or lng is None:
        return None, None
    if str(gps.get(ExifTags.GPS.GPSLatitudeRef, "N")).upper().startswith("S"):
        lat = -lat
    if str(gps.get(ExifTags.GPS.GPSLongitudeRef, "E")).upper().startswith("W"):
        lng = -lng
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None, None  # (0, 0) is what some phones write when they have no fix
    return round(lat, 7), round(lng, 7)


def _offset(value: Any) -> timezone | None:
    """EXIF OffsetTime* looks like '+05:30'."""
    try:
        text = str(value).strip()
        sign = -1 if text.startswith("-") else 1
        hours, minutes = text.lstrip("+-").split(":")
        return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))
    except (ValueError, AttributeError):
        return None


def read_taken_at(exif: Image.Exif, default_tz: str) -> datetime | None:
    details = exif.get_ifd(ExifTags.IFD.Exif)
    raw = details.get(ExifTags.Base.DateTimeOriginal) or exif.get(ExifTags.Base.DateTime)
    if not raw:
        return None
    try:
        taken = datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    tz = _offset(details.get(ExifTags.Base.OffsetTimeOriginal)) or ZoneInfo(default_tz)
    return taken.replace(tzinfo=tz)


def _jpeg(image: Image.Image, max_px: int, quality: int) -> bytes:
    copy = image.copy()
    copy.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    if copy.mode != "RGB":
        background = Image.new("RGB", copy.size, (255, 255, 255))
        rgba = copy.convert("RGBA")
        background.paste(rgba, mask=rgba.getchannel("A"))
        copy = background
    out = io.BytesIO()
    # No exif= argument, so nothing from the original (GPS, camera serial) is written.
    copy.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def process_image(
    data: bytes,
    declared_type: str,
    *,
    public_max_px: int = 1600,
    thumb_max_px: int = 400,
    default_tz: str = "Asia/Kolkata",
) -> Processed:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()  # catches truncated/corrupt files
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise NotAnImage("NOT_AN_IMAGE") from exc
    if ALLOWED_FORMATS.get(image.format or "") != declared_type:
        raise NotAnImage("TYPE_MISMATCH")

    exif = image.getexif()
    gps_lat, gps_lng = read_gps(exif)
    taken_at = read_taken_at(exif, default_tz)
    upright = ImageOps.exif_transpose(image)  # phone photos are often stored rotated
    return Processed(
        width=upright.width,
        height=upright.height,
        taken_at=taken_at,
        gps_lat=gps_lat,
        gps_lng=gps_lng,
        public_jpeg=_jpeg(upright, public_max_px, 85),
        thumb_jpeg=_jpeg(upright, thumb_max_px, 80),
    )
