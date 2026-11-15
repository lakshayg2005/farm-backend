"""Internal routes: reachable only inside the Docker network, never through the gateway."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.auth import decode_access_token
from farm_common.errors import Unauthorized
from identity_service.database import get_session
from identity_service.schemas import UserOut
from identity_service.service import get_user
from identity_service.settings import IdentitySettings, get_identity_settings

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/users/{user_id}")
async def get_user_internal(user_id: UUID, session: AsyncSession = Depends(get_session)) -> UserOut:
    """For services that need a user's details right now (e.g. a farmer's name on a receipt)."""
    return UserOut.model_validate(await get_user(session, user_id))


@router.get("/auth/verify")
async def forward_auth(
    authorization: str | None = Header(default=None),
    settings: IdentitySettings = Depends(get_identity_settings),
) -> Response:
    """Traefik forward-auth target: 200 + X-User-Id / X-User-Roles headers, or 401."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized()
    user = decode_access_token(token, settings)
    return Response(
        status_code=200,
        headers={"X-User-Id": user.id, "X-User-Roles": ",".join(user.roles)},
    )
