"""Idempotency-Key support for write calls (HLD sections 6 and 15).

A retry on a weak mobile connection with the same key gets the first response back instead of
creating a second upload. The stored response is written in the same transaction as the
created row, so either both exist or neither does.
"""

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.errors import Conflict
from media_service.db import IdempotencyKey


def request_hash(route: str, body: BaseModel) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{route}\n{canonical}".encode()).hexdigest()


async def replay(
    session: AsyncSession, user_id: str, key: str | None, digest: str
) -> JSONResponse | None:
    """The stored response for this key, or None if the key is new (or not given)."""
    if key is None:
        return None
    stored = await session.get(IdempotencyKey, (UUID(user_id), key))
    if stored is None:
        return None
    if stored.request_hash != digest:
        raise Conflict(
            "This Idempotency-Key was already used for a different request",
            code="IDEMPOTENCY_KEY_REUSED",
        )
    return JSONResponse(stored.response, status_code=stored.status_code)


def remember(
    session: AsyncSession,
    user_id: str,
    key: str | None,
    digest: str,
    status_code: int,
    response: BaseModel,
) -> None:
    if key is not None:
        body: dict[str, Any] = response.model_dump(mode="json", by_alias=True)
        session.add(
            IdempotencyKey(
                user_id=UUID(user_id),
                key=key,
                request_hash=digest,
                status_code=status_code,
                response=body,
            )
        )


async def commit_or_replay(
    session: AsyncSession, user_id: str, key: str | None, digest: str
) -> JSONResponse | None:
    """Commit; if a parallel request with the same key won the race, return its response."""
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if key is None:
            raise
        winner = await replay(session, user_id, key, digest)
        if winner is None:
            raise
        return winner
    return None
