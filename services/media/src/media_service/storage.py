"""Object storage behind a small interface: S3 (MinIO) in real runs, in memory for tests.

Key layout in the bucket:
    originals/<media id>.<ext>   private: the uploaded file, with its EXIF/GPS
    public/<media id>.jpg        public: re-encoded, EXIF stripped (GPS never leaves the server)
    thumbs/<media id>.jpg        public: small thumbnail
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from media_service.settings import MediaSettings, get_media_settings

logger = logging.getLogger(__name__)

PUBLIC_PREFIXES = ("public/", "thumbs/")


@dataclass
class ObjectInfo:
    size: int
    content_type: str | None


class ObjectStore(Protocol):
    def presign_put(self, key: str, content_type: str, size: int, expires_in: int) -> str: ...

    def presign_get(self, key: str, expires_in: int) -> str: ...

    def public_url(self, key: str) -> str: ...

    async def head(self, key: str) -> ObjectInfo | None: ...

    async def get(self, key: str) -> bytes: ...

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def delete(self, *keys: str) -> None: ...

    async def ensure_bucket(self) -> None: ...


def _client(settings: MediaSettings, endpoint: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


class S3ObjectStore:
    def __init__(self, settings: MediaSettings) -> None:
        self.bucket = settings.s3_bucket
        self.base_url = settings.public_base_url
        self._s3 = _client(settings, settings.s3_endpoint)
        # Upload URLs are signed for the host the client will use, which may differ from the
        # Docker-internal one (e.g. http://localhost:9000 vs http://minio:9000).
        self._signer = _client(settings, settings.public_endpoint)

    def presign_put(self, key: str, content_type: str, size: int, expires_in: int) -> str:
        # Content-Type and Content-Length are signed: the upload must match what was declared.
        return str(
            self._signer.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ContentType": content_type,
                    "ContentLength": size,
                },
                ExpiresIn=expires_in,
                HttpMethod="PUT",
            )
        )

    def presign_get(self, key: str, expires_in: int) -> str:
        return str(
            self._signer.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_in
            )
        )

    def public_url(self, key: str) -> str:
        return f"{self.base_url}/{key}"

    async def head(self, key: str) -> ObjectInfo | None:
        try:
            meta = await asyncio.to_thread(self._s3.head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return ObjectInfo(size=int(meta["ContentLength"]), content_type=meta.get("ContentType"))

    async def get(self, key: str) -> bytes:
        response = await asyncio.to_thread(self._s3.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(response["Body"].read)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
        )

    async def delete(self, *keys: str) -> None:
        if keys:
            await asyncio.to_thread(
                self._s3.delete_objects,
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
            )

    async def ensure_bucket(self) -> None:
        """Create the bucket if needed and let anyone read public/ and thumbs/ (not originals/)."""
        try:
            await asyncio.to_thread(self._s3.head_bucket, Bucket=self.bucket)
        except ClientError:
            await asyncio.to_thread(self._s3.create_bucket, Bucket=self.bucket)
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{self.bucket}/{p}*" for p in PUBLIC_PREFIXES],
                }
            ],
        }
        try:
            await asyncio.to_thread(
                self._s3.put_bucket_policy, Bucket=self.bucket, Policy=json.dumps(policy)
            )
        except ClientError:
            logger.warning("Could not set the public-read policy on %s", self.bucket)


class MemoryObjectStore:
    """In-memory store for tests and for running without object storage."""

    def __init__(self, base_url: str = "http://media.test/farm-media") -> None:
        self.base_url = base_url
        self.objects: dict[str, tuple[bytes, str]] = {}

    def presign_put(self, key: str, content_type: str, size: int, expires_in: int) -> str:
        return f"{self.base_url}/{key}?signed=put&expires={expires_in}"

    def presign_get(self, key: str, expires_in: int) -> str:
        return f"{self.base_url}/{key}?signed=get&expires={expires_in}"

    def public_url(self, key: str) -> str:
        return f"{self.base_url}/{key}"

    async def head(self, key: str) -> ObjectInfo | None:
        if key not in self.objects:
            return None
        data, content_type = self.objects[key]
        return ObjectInfo(size=len(data), content_type=content_type)

    async def get(self, key: str) -> bytes:
        return self.objects[key][0]

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.objects.pop(key, None)

    async def ensure_bucket(self) -> None:
        return None


@lru_cache
def get_store() -> ObjectStore:
    return S3ObjectStore(get_media_settings())
