from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends
from fastapi.testclient import TestClient

from farm_common.app import create_app
from farm_common.auth import CurrentUser, Role, get_current_user, require_roles
from farm_common.settings import ServiceSettings, get_settings


def _key_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


PRIVATE, PUBLIC = _key_pair()
OTHER_PRIVATE, _ = _key_pair()


def token(roles: list[str], *, key: str = PRIVATE, expires_in: int = 900, kind: str = "access"):
    now = datetime.now(UTC)
    claims = {
        "sub": "user-1",
        "roles": roles,
        "type": kind,
        "iss": "farm-identity",
        "aud": "farm-platform",
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def client() -> TestClient:
    settings = ServiceSettings(jwt_public_key=PUBLIC)
    app = create_app("demo", "0.0.1", settings, configure_logs=False)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/me")
    async def me(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        return user

    @app.post("/listings")
    async def create_listing(user: CurrentUser = Depends(require_roles(Role.FARMER))) -> dict:
        return {"created_by": user.id}

    return TestClient(app)


def auth(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


def test_missing_token_is_401_in_standard_error_shape(client):
    response = client.get("/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_valid_token_returns_user(client):
    response = client.get("/me", headers=auth(token(["farmer", "customer"])))

    assert response.status_code == 200
    assert response.json() == {"id": "user-1", "roles": ["farmer", "customer"]}


def test_role_allowed(client):
    response = client.post("/listings", headers=auth(token(["farmer"])))

    assert response.status_code == 200
    assert response.json() == {"created_by": "user-1"}


def test_role_forbidden(client):
    response = client.post("/listings", headers=auth(token(["customer"])))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    ("bad_token", "code"),
    [
        (token(["farmer"], expires_in=-10), "TOKEN_EXPIRED"),
        (token(["farmer"], key=OTHER_PRIVATE), "INVALID_TOKEN"),
        (token(["farmer"], kind="refresh"), "INVALID_TOKEN"),
        (token(["superuser"]), "INVALID_TOKEN"),
        ("not-a-jwt", "INVALID_TOKEN"),
    ],
)
def test_rejected_tokens(client, bad_token, code):
    response = client.get("/me", headers=auth(bad_token))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == code
