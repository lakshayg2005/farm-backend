from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from catalog_service.main import app
from farm_common.settings import ServiceSettings, get_settings

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


@pytest.fixture
def settings() -> ServiceSettings:
    return ServiceSettings(jwt_public_key=PUBLIC_KEY)


@pytest.fixture
async def client(settings) -> AsyncIterator[AsyncClient]:
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides = saved


@pytest.fixture
def farmer() -> dict[str, str]:
    return token_for(uuid4(), "farmer")


@pytest.fixture
def customer() -> dict[str, str]:
    return token_for(uuid4(), "customer")
