"""Reading EXIF (capture time, GPS) with exifread and making the public copy and thumbnail with
Pillow (HLD section 8: "Pillow + exifread")."""

import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import exifread
from PIL import Image, ImageOps, UnidentifiedImageError

# Refuse decompression bombs (a tiny file that expands to a gigantic bitmap).
Image.MAX_IMAGE_PIXELS = 50_000_000
ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class NotAnImage(ValueError):
    pass


@dataclass
class ExifData:
    taken_at: datetime | None = None
    gps_lat: float | None = None
    gps_lng: float | None = None


@dataclass
class Processed:
    width: int
    height: int
    exif: ExifData
    public_jpeg: bytes
    thumb_jpeg: bytes


def _number(value: Any) -> float:
    if hasattr(value, "num") and hasattr(value, "den"):
        return float(value.num) / float(value.den) if value.den else 0.0
    return float(value)


def _degrees(tag: Any) -> float | None:
    try:
        d, m, s = (_number(v) for v in tag.values)
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None
    return d + m / 60 + s / 3600


def _text(tag: Any) -> str:
    return str(getattr(tag, "printable", tag or "")).strip("\x00 ")


def _offset(value: str) -> timezone | None:
    """EXIF OffsetTime* looks like '+05:30'."""
    try:
        sign = -1 if value.startswith("-") else 1
        hours, minutes = value.lstrip("+-").split(":")
        return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))
    except ValueError:
        return None


def read_exif(data: bytes, default_tz: str) -> ExifData:
    try:
        tags = exifread.process_file(io.BytesIO(data), details=False)
    except Exception:  # exifread raises assorted errors on odd files; EXIF is optional
        return ExifData()
    out = ExifData()

    lat = _degrees(tags.get("GPS GPSLatitude"))
    lng = _degrees(tags.get("GPS GPSLongitude"))
    if lat is not None and lng is not None:
        if _text(tags.get("GPS GPSLatitudeRef", "N")).upper().startswith("S"):
            lat = -lat
        if _text(tags.get("GPS GPSLongitudeRef", "E")).upper().startswith("W"):
            lng = -lng
        # (0, 0) is what some phones write when they have no fix
        if -90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0):
            out.gps_lat, out.gps_lng = round(lat, 7), round(lng, 7)

    raw = _text(tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime"))
    if raw:
        try:
            taken = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
        except ValueError:
            taken = None
        if taken is not None:
            tz = _offset(_text(tags.get("EXIF OffsetTimeOriginal"))) or ZoneInfo(default_tz)
            out.taken_at = taken.replace(tzinfo=tz)
    return out


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

    upright = ImageOps.exif_transpose(image)  # phone photos are often stored rotated
    return Processed(
        width=upright.width,
        height=upright.height,
        exif=read_exif(data, default_tz),
        public_jpeg=_jpeg(upright, public_max_px, 85),
        thumb_jpeg=_jpeg(upright, thumb_max_px, 80),
    )
