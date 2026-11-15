from functools import lru_cache

from farm_common.settings import ServiceSettings


class MediaSettings(ServiceSettings):
    # Object storage (MinIO locally, any S3-compatible store when deployed).
    s3_endpoint: str = "http://localhost:9000"  # used by the service itself (Docker network)
    s3_public_endpoint: str | None = None  # what browsers/apps reach; signs upload URLs
    s3_access_key: str = "farm"
    s3_secret_key: str = "change-me-too"
    s3_region: str = "us-east-1"
    s3_bucket: str = "farm-media"
    # Base URL for public copies and thumbnails; default <public endpoint>/<bucket>.
    media_public_base_url: str | None = None

    upload_url_ttl_seconds: int = 300
    max_upload_bytes: int = 5 * 1024 * 1024
    public_max_px: int = 1600
    thumb_max_px: int = 400
    geo_verify_radius_m: float = 2000
    # EXIF times usually have no time zone; read them as this zone.
    exif_default_tz: str = "Asia/Kolkata"

    # Where photo locations are checked against (internal routes, see geo.py).
    catalog_internal_url: str = "http://catalog:8000"
    land_internal_url: str = "http://land:8000"

    events_enabled: bool = True
    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0
    worker_max_attempts: int = 3

    @property
    def public_endpoint(self) -> str:
        return (self.s3_public_endpoint or self.s3_endpoint).rstrip("/")

    @property
    def public_base_url(self) -> str:
        return (self.media_public_base_url or f"{self.public_endpoint}/{self.s3_bucket}").rstrip(
            "/"
        )


@lru_cache
def get_media_settings() -> MediaSettings:
    return MediaSettings()
