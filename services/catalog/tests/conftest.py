"""Catalog tests run against real PostgreSQL + PostGIS (geo search and row locking can't be faked).

Start one and point TEST_DATABASE_URL at it, e.g.

    docker run -d --rm --name farm-test-db -p 55433:5432 -e POSTGRES_USER=farm \\
        -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=farm_test postgis/postgis:16-3.4
    export TEST_DATABASE_URL=postgresql+asyncpg://farm:change-me@localhost:55433/farm_test

Without it the database tests are skipped locally (and fail in CI). The fixture drops and
re-creates the `catalog` schema with the real Alembic migrations, so never point it at real data.
"""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from catalog_service.database import get_session
from catalog_service.db import OutboxEvent
from catalog_service.main import app
from catalog_service.settings import CatalogSettings, get_catalog_settings
from farm_common.settings import get_settings

SERVICE_DIR = Path(__file__).resolve().parents[1]
TABLES = "outbox, processed_events, idempotency_keys, stock_movements, stock_reservations, produce_listings, farms"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY = _KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUBLIC_KEY = (
    _KEY.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)


def token_for(user_id: UUID, *roles: str) -> dict[str, str]:
    """Authorization header for a user, signed like the identity service signs tokens."""
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "roles": list(roles),
        "type": "access",
        "iss": "farm-identity",
        "aud": "farm-platform",
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, PRIVATE_KEY, algorithm='RS256')}"}


async def _drop_schema(url: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS catalog CASCADE"))
    await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        message = "set TEST_DATABASE_URL to a PostGIS database to run the catalog tests"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    asyncio.run(_drop_schema(url))
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=SERVICE_DIR,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    return url


@pytest.fixture
def settings() -> CatalogSettings:
    return CatalogSettings(jwt_public_key=PUBLIC_KEY, events_enabled=False)


@pytest.fixture
async def session_factory(database_url) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_size=60, max_overflow=0)
    async with engine.begin() as conn:
        await conn.execute(
            text(f"TRUNCATE {', '.join(f'catalog.{t}' for t in TABLES.split(', '))}")
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def client(settings, session_factory) -> AsyncIterator[AsyncClient]:
    async def session_override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    saved = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_session: session_override,
            get_settings: lambda: settings,
            get_catalog_settings: lambda: settings,
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides = saved


@pytest.fixture
def outbox(session_factory):
    async def read(event_type: str | None = None) -> list[OutboxEvent]:
        async with session_factory() as session:
            query = select(OutboxEvent).order_by(OutboxEvent.occurred_at)
            if event_type:
                query = query.where(OutboxEvent.type == event_type)
            return list((await session.execute(query)).scalars())

    return read


# Durg, Chhattisgarh. 0.009 degrees of latitude is about 1 km.
HOME = (21.19, 81.28)


def point(lat: float, lng: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lng, lat]}


class Api:
    """Helpers: a farmer with a farm, listing creation, reserve/release calls."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        self.farmer_id = uuid4()
        self.farmer = token_for(self.farmer_id, "farmer")

    async def farm(self, lat: float = HOME[0], lng: float = HOME[1], **extra: Any) -> dict:
        body = {"name": "Sahu Farm", "district": "Durg", "location": point(lat, lng), **extra}
        response = await self.client.post("/farms", json=body, headers=self.farmer)
        assert response.status_code == 201, response.text
        return response.json()

    async def listing(self, farm: dict | None = None, **extra: Any) -> dict:
        farm = farm or await self.farm()
        body = {
            "farmId": farm["id"],
            "cropId": "tomato",
            "qtyAvailable": 100,
            "pricePerUnit": 30,
            "harvestDate": "2026-11-14",
            **extra,
        }
        response = await self.client.post("/listings", json=body, headers=self.farmer)
        assert response.status_code == 201, response.text
        return response.json()

    async def reserve(self, order_id: UUID | str, *items: tuple[str, float]):
        body = {
            "orderId": str(order_id),
            "items": [{"listingId": lid, "qty": qty} for lid, qty in items],
        }
        return await self.client.post("/internal/stock/reserve", json=body)

    async def release(self, order_id: UUID | str):
        return await self.client.post("/internal/stock/release", json={"orderId": str(order_id)})

    async def get(self, listing_id: str) -> dict:
        response = await self.client.get(f"/listings/{listing_id}")
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def make_token():
    return token_for


@pytest.fixture
def home() -> tuple[float, float]:
    return HOME


@pytest.fixture
def api(client) -> Api:
    return Api(client)
