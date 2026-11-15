import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from PIL import ExifTags, Image
from shapely.geometry import Point, box
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from farm_common.settings import get_settings
from media_service.database import get_session
from media_service.db import Base, OutboxEvent
from media_service.geo import GeoReference
from media_service.main import app
from media_service.schemas import OwnerType
from media_service.settings import MediaSettings, get_media_settings
from media_service.storage import MemoryObjectStore, get_store
from media_service.worker import process_one

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY = _KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUBLIC_KEY = (
    _KEY.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)

# A farm in Durg with a ~1 km square boundary.
FARM_POINT = (21.19, 81.28)
FARM_BOX = box(81.275, 21.185, 81.285, 21.195)


def token_for(user_id: UUID, *roles: str) -> dict[str, str]:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "roles": list(roles),
        "type": "access",
        "iss": "farm-identity",
        "aud": "farm-platform",
        "exp": now + timedelta(minutes=15),
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, PRIVATE_KEY, algorithm='RS256')}"}


def _dms(value: float) -> tuple[float, float, float]:
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = round((value - degrees - minutes / 60) * 3600, 4)
    return (float(degrees), float(minutes), seconds)


def photo(
    gps: tuple[float, float] | None = None,
    taken: str | None = "2026:11:14 07:45:00",
    fmt: str = "JPEG",
    size: tuple[int, int] = (2400, 1800),
    orientation: int | None = None,
) -> bytes:
    """A camera-like image; `gps` is (lat, lng) written into EXIF."""
    image = Image.new("RGB", size, (90, 140, 60))
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "TestPhone"
    if taken:
        exif[ExifTags.Base.DateTime] = taken
        exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = taken
    if orientation:
        exif[ExifTags.Base.Orientation] = orientation
    if gps:
        lat, lng = gps
        exif.get_ifd(ExifTags.IFD.GPSInfo).update(
            {
                ExifTags.GPS.GPSLatitudeRef: "N" if lat >= 0 else "S",
                ExifTags.GPS.GPSLatitude: _dms(lat),
                ExifTags.GPS.GPSLongitudeRef: "E" if lng >= 0 else "W",
                ExifTags.GPS.GPSLongitude: _dms(lng),
            }
        )
    out = io.BytesIO()
    image.save(out, fmt, exif=exif)
    return out.getvalue()


class FakeResolver:
    def __init__(self) -> None:
        self.places: dict[UUID, GeoReference] = {}
        self.calls: list[tuple[OwnerType, UUID]] = []

    async def locate(self, owner_type: OwnerType, owner_id: UUID) -> GeoReference | None:
        self.calls.append((owner_type, owner_id))
        return self.places.get(owner_id)


@pytest.fixture
def settings() -> MediaSettings:
    return MediaSettings(jwt_public_key=PUBLIC_KEY, events_enabled=False, worker_enabled=False)


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver()


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # SQLite has no schemas, so the `media` schema is mapped to the default one.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        execution_options={"schema_translate_map": {"media": None}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def client(settings, session_factory, store) -> AsyncIterator[AsyncClient]:
    async def session_override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    saved = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_session: session_override,
            get_settings: lambda: settings,
            get_media_settings: lambda: settings,
            get_store: lambda: store,
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides = saved


@pytest.fixture
def outbox(session_factory):
    async def read() -> list[OutboxEvent]:
        async with session_factory() as session:
            rows = await session.execute(select(OutboxEvent).order_by(OutboxEvent.occurred_at))
            return list(rows.scalars())

    return read


class Uploader:
    """Runs the whole client flow: upload-url -> PUT -> complete -> worker."""

    def __init__(self, client, store, resolver, session_factory, settings) -> None:
        self.client, self.store, self.resolver = client, store, resolver
        self.session_factory, self.settings = session_factory, settings
        self.user_id = uuid4()
        self.headers = token_for(self.user_id, "farmer")
        self.farm_id = uuid4()
        resolver.places[self.farm_id] = GeoReference(
            point=Point(FARM_POINT[1], FARM_POINT[0]), boundary=FARM_BOX
        )

    async def request(self, data: bytes, content_type: str = "image/jpeg", **extra) -> dict:
        body = {
            "ownerType": "FARM",
            "ownerId": str(self.farm_id),
            "contentType": content_type,
            "sizeBytes": len(data),
            **extra,
        }
        response = await self.client.post("/upload-url", json=body, headers=self.headers)
        assert response.status_code == 201, response.text
        return response.json()

    async def put(self, ticket: dict, data: bytes, content_type: str = "image/jpeg") -> None:
        key = ticket["uploadUrl"].split("/farm-media/")[1].split("?")[0]
        await self.store.put(key, data, content_type)

    async def complete(self, media_id: str):
        return await self.client.post(f"/{media_id}/complete", headers=self.headers)

    async def process(self) -> bool:
        return await process_one(self.session_factory, self.store, self.resolver, self.settings)

    async def upload(self, data: bytes, content_type: str = "image/jpeg", **extra) -> dict:
        ticket = await self.request(data, content_type, **extra)
        await self.put(ticket, data, content_type)
        completed = await self.complete(ticket["mediaId"])
        assert completed.status_code == 202, completed.text
        assert await self.process()
        response = await self.client.get(f"/{ticket['mediaId']}")
        return response.json()


@pytest.fixture
def uploader(client, store, resolver, session_factory, settings) -> Uploader:
    return Uploader(client, store, resolver, session_factory, settings)


@pytest.fixture
def make_photo():
    return photo


@pytest.fixture
def make_token():
    return token_for
