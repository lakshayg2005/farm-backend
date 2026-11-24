"""Public routes (behind the gateway at /api/v1/identity)."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.auth import CurrentUser, get_current_user
from farm_common.errors import AppError, Conflict, Unauthorized
from identity_service.database import get_session
from identity_service.db import OtpCode, RefreshToken, User
from identity_service.schemas import (
    AddRoleIn,
    LoginIn,
    OtpRequestIn,
    OtpRequestOut,
    OtpVerifyIn,
    PublicKeyOut,
    RefreshIn,
    RegisterIn,
    TokenOut,
    UpdateMeIn,
    UserOut,
)
from identity_service.security import (
    DUMMY_PASSWORD_HASH,
    decode_refresh_token,
    generate_otp,
    hash_otp,
    hash_password_async,
    otp_matches,
    verify_password_async,
)
from identity_service.service import (
    find_by_phone,
    get_user,
    publish,
    revoke_all_refresh_tokens,
    start_session,
    user_payload,
)
from identity_service.settings import IdentitySettings, get_identity_settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _as_utc(value: datetime) -> datetime:
    # SQLite (tests) returns naive datetimes; Postgres returns aware ones
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@router.post("/register", status_code=status.HTTP_201_CREATED, tags=["auth"])
async def register(body: RegisterIn, session: AsyncSession = Depends(get_session)) -> UserOut:
    if await find_by_phone(session, body.phone):
        raise Conflict("This phone number is already registered", code="PHONE_TAKEN")
    if (
        body.email
        and (await session.execute(select(User.id).where(User.email == body.email))).first()
    ):
        raise Conflict("This email is already registered", code="EMAIL_TAKEN")

    user = User(
        name=body.name,
        phone=body.phone,
        email=body.email,
        password_hash=await hash_password_async(body.password),
        home_lat=body.home_lat,
        home_lng=body.home_lng,
        role_rows=[],
    )
    for role in body.roles:
        user.add_role(role)
    session.add(user)
    await session.flush()
    publish(session, "user.registered", user_payload(user))
    try:
        await session.commit()
    except IntegrityError as exc:  # lost a race with an identical sign-up
        raise Conflict("This phone or email is already registered", code="ACCOUNT_EXISTS") from exc
    return UserOut.model_validate(user)


@router.post("/login", tags=["auth"])
async def login(
    body: LoginIn,
    session: AsyncSession = Depends(get_session),
    settings: IdentitySettings = Depends(get_identity_settings),
) -> TokenOut:
    user = await find_by_phone(session, body.phone)
    if user is None:
        await verify_password_async(body.password, DUMMY_PASSWORD_HASH)
        raise Unauthorized("Wrong phone number or password", code="INVALID_CREDENTIALS")
    if not await verify_password_async(body.password, user.password_hash):
        raise Unauthorized("Wrong phone number or password", code="INVALID_CREDENTIALS")
    return await start_session(session, user, settings)


@router.post("/otp/request", status_code=status.HTTP_202_ACCEPTED, tags=["auth"])
async def request_otp(
    body: OtpRequestIn,
    session: AsyncSession = Depends(get_session),
    settings: IdentitySettings = Depends(get_identity_settings),
) -> OtpRequestOut:
    """Send a 6-digit code to the phone (verifies the number and also works as passwordless login)."""
    if await find_by_phone(session, body.phone) is None:
        raise AppError("USER_NOT_FOUND", "No account with this phone number", 404)
    now = datetime.now(UTC)
    last = (
        await session.execute(
            select(OtpCode).where(OtpCode.phone == body.phone).order_by(OtpCode.created_at.desc())
        )
    ).scalar()
    if last and now - _as_utc(last.created_at) < timedelta(seconds=settings.otp_resend_seconds):
        raise AppError("OTP_TOO_SOON", "Please wait before requesting another code", 429)

    code = generate_otp()
    session.add(
        OtpCode(
            phone=body.phone,
            code_hash=hash_otp(body.phone, code),
            expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
            created_at=now,
        )
    )
    await session.commit()
    if settings.otp_dev_mode:
        logger.info("Mock SMS to %s: your Farm Platform code is %s", body.phone, code)
    return OtpRequestOut(
        expires_in=settings.otp_ttl_seconds,
        resend_after=settings.otp_resend_seconds,
        dev_code=code if settings.otp_dev_mode else None,
    )


@router.post("/otp/verify", tags=["auth"])
async def verify_otp(
    body: OtpVerifyIn,
    session: AsyncSession = Depends(get_session),
    settings: IdentitySettings = Depends(get_identity_settings),
) -> TokenOut:
    now = datetime.now(UTC)
    otp = (
        await session.execute(
            select(OtpCode)
            .where(OtpCode.phone == body.phone, OtpCode.consumed_at.is_(None))
            .order_by(OtpCode.created_at.desc())
        )
    ).scalar()
    if otp is None or _as_utc(otp.expires_at) < now:
        raise AppError("OTP_INVALID", "The code is wrong or has expired", 400)
    if otp.attempts >= settings.otp_max_attempts:
        raise AppError("OTP_LOCKED", "Too many wrong attempts; request a new code", 429)
    if not otp_matches(body.phone, body.code, otp.code_hash):
        otp.attempts += 1
        await session.commit()
        raise AppError("OTP_INVALID", "The code is wrong or has expired", 400)

    otp.consumed_at = now
    user = await find_by_phone(session, body.phone)
    if user is None:
        raise AppError("USER_NOT_FOUND", "No account with this phone number", 404)
    user.phone_verified = True
    return await start_session(session, user, settings)


@router.post("/refresh", tags=["auth"])
async def refresh(
    body: RefreshIn,
    session: AsyncSession = Depends(get_session),
    settings: IdentitySettings = Depends(get_identity_settings),
) -> TokenOut:
    """Swap a refresh token for a new pair. Each refresh token works once (rotation)."""
    user_id, jti = decode_refresh_token(body.refresh_token, settings)
    stored = await session.get(RefreshToken, jti)
    if stored is None or stored.user_id != user_id:
        raise Unauthorized("Invalid refresh token", code="INVALID_TOKEN")
    if stored.revoked_at is not None:
        # A used token came back: it may have been stolen, so end every session of this user.
        await revoke_all_refresh_tokens(session, user_id)
        await session.commit()
        raise Unauthorized("Refresh token was already used", code="TOKEN_REUSED")
    stored.revoked_at = datetime.now(UTC)
    user = await get_user(session, user_id)
    return await start_session(session, user, settings)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
async def logout(
    body: RefreshIn,
    session: AsyncSession = Depends(get_session),
    settings: IdentitySettings = Depends(get_identity_settings),
) -> Response:
    _, jti = decode_refresh_token(body.refresh_token, settings)
    stored = await session.get(RefreshToken, jti)
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = datetime.now(UTC)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", tags=["me"])
async def me(
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    return UserOut.model_validate(await get_user(session, UUID(current.id)))


@router.patch("/me", tags=["me"])
async def update_me(
    body: UpdateMeIn,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    user = await get_user(session, UUID(current.id))
    changes = body.model_dump(exclude_unset=True)
    if "email" in changes and changes["email"] and changes["email"] != user.email:
        taken = (
            await session.execute(select(User.id).where(User.email == changes["email"]))
        ).first()
        if taken:
            raise Conflict("This email is already registered", code="EMAIL_TAKEN")
    for field, value in changes.items():
        setattr(user, field, value)
    publish(session, "user.updated", user_payload(user))
    await session.commit()
    return UserOut.model_validate(user)


@router.post("/me/roles", tags=["me"])
async def add_my_role(
    body: AddRoleIn,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    """Become a farmer, customer or delivery partner too. Call /refresh to get the new role in the token."""
    user = await get_user(session, UUID(current.id))
    if user.add_role(body.role):
        publish(session, "user.roles_changed", user_payload(user))
        await session.commit()
    return UserOut.model_validate(user)


@router.get("/public-key", tags=["auth"])
async def public_key(settings: IdentitySettings = Depends(get_identity_settings)) -> PublicKeyOut:
    """The key other services use to verify access tokens."""
    settings.private_key()  # fail loudly if identity has no key configured
    return PublicKeyOut(
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        public_key=settings.jwt_public_key or "",
    )
