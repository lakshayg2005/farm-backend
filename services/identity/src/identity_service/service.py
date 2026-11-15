"""Operations shared by the public, admin and internal routes."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.errors import NotFound
from farm_common.events import Event
from farm_common.outbox import add_event
from identity_service.db import OutboxEvent, RefreshToken, User
from identity_service.schemas import TokenOut, UserOut
from identity_service.security import issue_tokens
from identity_service.settings import IdentitySettings

PRODUCER = "identity"


async def get_user(session: AsyncSession, user_id: UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("User not found", code="USER_NOT_FOUND")
    return user


async def find_by_phone(session: AsyncSession, phone: str) -> User | None:
    return (await session.execute(select(User).where(User.phone == phone))).scalar_one_or_none()


def publish(session: AsyncSession, event_type: str, payload: dict) -> None:
    add_event(session, OutboxEvent, Event(type=event_type, producer=PRODUCER, payload=payload))


def user_payload(user: User) -> dict:
    """What other services may copy about a user (no password hash, no tokens)."""
    return {
        "userId": str(user.id),
        "name": user.name,
        "phone": user.phone,
        "roles": user.roles,
        "farmerVerified": user.farmer_verified,
        "homeLat": user.home_lat,
        "homeLng": user.home_lng,
    }


async def start_session(session: AsyncSession, user: User, settings: IdentitySettings) -> TokenOut:
    """Issue an access + refresh token pair and remember the refresh token. Commits."""
    tokens = issue_tokens(user.id, user.roles, settings)
    session.add(
        RefreshToken(id=tokens.refresh_jti, user_id=user.id, expires_at=tokens.refresh_expires_at)
    )
    await session.commit()
    return TokenOut(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user=UserOut.model_validate(user),
    )


async def revoke_all_refresh_tokens(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
