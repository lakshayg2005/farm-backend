"""Nearest-inspector assignment against real PostGIS (row locks and KNN order can't be faked).

Needs TEST_DATABASE_URL (see services/catalog/tests/conftest.py); skipped without it locally.
"""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from quality_service.consumers import create_inspection
from quality_service.db import Base, InspectorLocation
from quality_service.geo import point_to_db
from quality_service.schemas import Point


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        message = "set TEST_DATABASE_URL to a PostGIS database to run this test"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.execute(text("DROP SCHEMA IF EXISTS quality CASCADE"))
        await conn.execute(text("CREATE SCHEMA quality"))
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS quality CASCADE"))
    await engine.dispose()


def _at(lng: float, lat: float):
    return point_to_db(Point(coordinates=(lng, lat)))


async def test_packed_orders_claim_nearest_free_inspector(sessions) -> None:
    near, far, busy = uuid4(), uuid4(), uuid4()
    async with sessions() as session, session.begin():
        session.add_all(
            [
                InspectorLocation(inspector_id=near, location=_at(81.63, 21.25)),
                InspectorLocation(inspector_id=far, location=_at(73.79, 20.00)),
                InspectorLocation(inspector_id=busy, location=_at(81.40, 21.23), available=False),
            ]
        )

    pickup = {"type": "Point", "coordinates": [81.40233, 21.23168]}
    claimed = []
    for _ in range(3):
        async with sessions() as session, session.begin():
            # the GiST index is what made PostgreSQL refuse the lock, so make sure it is used
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            row = await create_inspection(session, "ORDER", str(uuid4()), pickup)
            claimed.append(row.inspector_id)

    # busy is closest but not available; then near, then far; then nobody is left
    assert claimed == [near, far, None]
