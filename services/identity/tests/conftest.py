from collections.abc import AsyncIterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from farm_common.settings import get_settings
from identity_service.database import get_session
from identity_service.db import Base, OutboxEvent, User
from identity_service.main import app
from identity_service.security import hash_password
from identity_service.settings import IdentitySettings, get_identity_settings

PRIVATE_KEY = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode()
)
PASSWORD = "correct-horse-1"


@pytest.fixture
def settings() -> IdentitySettings:
    # Ignore a developer's .env: its JWT_PUBLIC_KEY_PATH would not match the test private key.
    return IdentitySettings(
        _env_file=None,
        jwt_private_key=PRIVATE_KEY,
        jwt_public_key_path=None,
        otp_resend_seconds=0,
        events_enabled=False,
    )


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # SQLite has no schemas, so the `identity` schema is mapped to the default one.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        execution_options={"schema_translate_map": {"identity": None}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
            get_identity_settings: lambda: settings,
            get_settings: lambda: settings,
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides = saved


@pytest.fixture
def outbox_types(session_factory):
    async def read() -> list[str]:
        async with session_factory() as session:
            rows = await session.execute(select(OutboxEvent.type).order_by(OutboxEvent.occurred_at))
            return list(rows.scalars())

    return read


@pytest.fixture
def make_admin(session_factory):
    async def create(phone: str = "+919000000001") -> None:
        async with session_factory() as session:
            user = User(
                name="Admin", phone=phone, password_hash=hash_password(PASSWORD), role_rows=[]
            )
            user.add_role("admin")
            session.add(user)
            await session.commit()

    return create


class Api:
    """Helpers for tests: `api.register(...)`, `tokens = await api.login()`, `api.bearer(tokens)`."""

    PASSWORD = PASSWORD

    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    async def register(self, phone: str = "9876543210", roles=("customer",), **extra):
        body = {"name": "Asha", "phone": phone, "password": PASSWORD, "roles": list(roles), **extra}
        return await self.client.post("/register", json=body)

    async def login(self, phone: str = "+919876543210") -> dict:
        response = await self.client.post("/login", json={"phone": phone, "password": PASSWORD})
        assert response.status_code == 200, response.text
        return response.json()

    @staticmethod
    def bearer(tokens: dict) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens['accessToken']}"}


@pytest.fixture
def api(client) -> Api:
    return Api(client)
