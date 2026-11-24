"""Passwords, OTP codes and token signing."""

import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bcrypt
import jwt

from farm_common.auth import public_key
from farm_common.errors import Unauthorized
from identity_service.settings import IdentitySettings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# bcrypt is deliberately slow (~0.25-0.5 s of CPU). Called directly from an async route it would
# freeze the whole service for that long, so every other identity request (token refresh, /me,
# the gateway's auth checks) waits behind each sign-in. In a thread it runs off the event loop,
# and since bcrypt releases the GIL several sign-ins use several cores.
async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(verify_password, password, password_hash)


# Checked against when the phone is unknown, so a failed login takes the same time either way.
DUMMY_PASSWORD_HASH = hash_password("not-a-real-password")


def generate_otp() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def hash_otp(phone: str, code: str) -> str:
    return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()


def otp_matches(phone: str, code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_otp(phone, code), code_hash)


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    refresh_jti: UUID
    refresh_expires_at: datetime
    expires_in: int


def issue_tokens(
    user_id: UUID, roles: list[str], settings: IdentitySettings, farmer_verified: bool = False
) -> IssuedTokens:
    now = datetime.now(UTC)
    common = {
        "sub": str(user_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
    }
    access_ttl = timedelta(minutes=settings.access_token_minutes)
    access = jwt.encode(
        # `fv`: the admin verified this farmer; catalog and land only let verified farmers sell
        {
            **common,
            "type": "access",
            "roles": roles,
            "fv": farmer_verified,
            "exp": now + access_ttl,
        },
        settings.private_key(),
        algorithm="RS256",
    )
    jti = uuid4()
    refresh_expires_at = now + timedelta(days=settings.refresh_token_days)
    refresh = jwt.encode(
        {**common, "type": "refresh", "jti": str(jti), "exp": refresh_expires_at},
        settings.private_key(),
        algorithm="RS256",
    )
    return IssuedTokens(
        access_token=access,
        refresh_token=refresh,
        refresh_jti=jti,
        refresh_expires_at=refresh_expires_at,
        expires_in=int(access_ttl.total_seconds()),
    )


def decode_refresh_token(token: str, settings: IdentitySettings) -> tuple[UUID, UUID]:
    """Return (user_id, jti) of a valid refresh token, or raise 401."""
    try:
        claims = jwt.decode(
            token,
            public_key(settings),
            algorithms=["RS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "jti"]},
        )
        if claims.get("type") != "refresh":
            raise jwt.InvalidTokenError("not a refresh token")
        return UUID(claims["sub"]), UUID(claims["jti"])
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise Unauthorized("Invalid refresh token", code="INVALID_TOKEN") from exc
