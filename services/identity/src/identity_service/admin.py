"""Admin routes: user lists, farmer verification, granting inspector/admin roles."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.auth import CurrentUser, Role, require_roles
from farm_common.errors import AppError
from identity_service.database import get_session
from identity_service.db import User, UserRole
from identity_service.schemas import SetRolesIn, UserOut, UserPage
from identity_service.service import get_user, publish, revoke_all_refresh_tokens, user_payload

router = APIRouter(prefix="/users", tags=["admin"])
admin_only = require_roles(Role.ADMIN)


@router.get("")
async def list_users(
    role: Role | None = None,
    farmer_verified: bool | None = Query(default=None, alias="farmerVerified"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    _: CurrentUser = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> UserPage:
    """Newest first. Pass `nextCursor` from the previous page as `cursor` to continue."""
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    query = select(User).order_by(User.created_at.desc(), User.id)
    if role is not None:
        query = query.join(UserRole).where(UserRole.role == role)
    if farmer_verified is not None:
        query = query.where(User.farmer_verified == farmer_verified)
    users = (await session.execute(query.offset(offset).limit(limit + 1))).scalars().all()
    has_more = len(users) > limit
    return UserPage(
        items=[UserOut.model_validate(u) for u in users[:limit]],
        next_cursor=str(offset + limit) if has_more else None,
    )


@router.get("/{user_id}")
async def get_one(
    user_id: UUID,
    _: CurrentUser = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    return UserOut.model_validate(await get_user(session, user_id))


@router.post("/{user_id}/verify-farmer")
async def verify_farmer(
    user_id: UUID,
    _: CurrentUser = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    user = await get_user(session, user_id)
    if Role.FARMER not in user.roles:
        raise AppError("NOT_A_FARMER", "This user is not registered as a farmer", 409)
    if not user.farmer_verified:
        user.farmer_verified = True
        publish(session, "farmer.verified", user_payload(user))
        await session.commit()
    return UserOut.model_validate(user)


@router.put("/{user_id}/roles")
async def set_roles(
    user_id: UUID,
    body: SetRolesIn,
    _: CurrentUser = Depends(admin_only),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    """Replace a user's roles (the only way to grant inspector or admin). Ends their sessions."""
    user = await get_user(session, user_id)
    user.set_roles([str(r) for r in body.roles])
    await revoke_all_refresh_tokens(session, user.id)
    publish(session, "user.roles_changed", user_payload(user))
    await session.commit()
    return UserOut.model_validate(user)
