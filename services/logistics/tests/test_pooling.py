"""Integration tests for the pooling engine (requires TEST_DATABASE_URL).

Tests the full run_pooling() flow: clustering → (mocked) OSRM → DB persist → outbox.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from logistics_service.db import DeliveryPool, OutboxEvent, PoolStop
from logistics_service.pooling import run_pooling
from logistics_service.routing import RouteResult
from logistics_service.settings import LogisticsSettings


def _order(lat: float, lng: float, weight: float = 5.0) -> dict[str, Any]:
    return {
        "order_id": uuid4(),
        "hub_lat": 21.0,
        "hub_lng": 81.0,
        "drop_lat": lat,
        "drop_lng": lng,
        "weight_kg": weight,
        "address": "Test address",
    }


POOL_DATE = date(2026, 11, 17)

_MOCK_ROUTE = RouteResult(
    total_km=10.5,
    km_saved=2.3,
    linestring_coords=[(21.0, 81.0), (21.1, 81.1)],
    waypoint_order=[0, 1],
    etas=[],
    osrm_meta={"fallback": True},
)


@pytest.mark.asyncio
async def test_no_orders_creates_no_pools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = LogisticsSettings(events_enabled=False)
    pools = await run_pooling(POOL_DATE, [], session_factory, settings)
    assert pools == []


@pytest.mark.asyncio
async def test_single_order_creates_one_pool(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = LogisticsSettings(events_enabled=False)
    orders = [_order(21.1, 81.3)]

    with patch(
        "logistics_service.pooling.get_route",
        new=AsyncMock(return_value=_MOCK_ROUTE),
    ):
        pools = await run_pooling(POOL_DATE, orders, session_factory, settings)

    assert len(pools) == 1
    # Verify DB row
    async with session_factory() as session:
        db_pool = await session.get(DeliveryPool, pools[0].id)
        assert db_pool is not None
        assert db_pool.total_km == pytest.approx(10.5)
        assert db_pool.km_saved == pytest.approx(2.3)

        result = await session.execute(select(PoolStop).where(PoolStop.pool_id == pools[0].id))
        stops = result.scalars().all()
        # 2 stops per order: PICKUP + DROP
        assert len(stops) == 2
        types = {s.stop_type for s in stops}
        assert "PICKUP" in types
        assert "DROP" in types


@pytest.mark.asyncio
async def test_pool_assigned_event_in_outbox(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = LogisticsSettings(events_enabled=False)
    orders = [_order(21.1, 81.3)]

    with patch(
        "logistics_service.pooling.get_route",
        new=AsyncMock(return_value=_MOCK_ROUTE),
    ):
        pools = await run_pooling(POOL_DATE, orders, session_factory, settings)

    async with session_factory() as session:
        result = await session.execute(
            select(OutboxEvent).where(OutboxEvent.type == "pool.assigned")
        )
        events = result.scalars().all()
        assert len(events) == len(pools)
        payload = events[0].payload
        assert "poolId" in payload
        assert "totalKm" in payload


@pytest.mark.asyncio
async def test_capacity_splitting_creates_multiple_pools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = LogisticsSettings(events_enabled=False, partner_capacity_kg=30.0)
    # Three 15 kg orders near each other → 2 pools (30 kg limit: 15+15, then 15)
    orders = [_order(21.100 + i * 0.001, 81.300, weight=15.0) for i in range(3)]

    with patch(
        "logistics_service.pooling.get_route",
        new=AsyncMock(return_value=_MOCK_ROUTE),
    ):
        pools = await run_pooling(POOL_DATE, orders, session_factory, settings)

    assert len(pools) == 2


@pytest.mark.asyncio
async def test_distant_orders_separate_pools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = LogisticsSettings(events_enabled=False, dbscan_eps_km=3.0)
    orders = [
        _order(21.1, 81.3),  # Raipur area
        _order(21.5, 81.7),  # ~60 km away
    ]

    with patch(
        "logistics_service.pooling.get_route",
        new=AsyncMock(return_value=_MOCK_ROUTE),
    ):
        pools = await run_pooling(POOL_DATE, orders, session_factory, settings)

    assert len(pools) == 2


@pytest.mark.asyncio
async def test_pickup_before_drop_ordering(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Validate issue #19 constraint: all PICKUP stops precede all DROP stops."""
    settings = LogisticsSettings(events_enabled=False)
    orders = [
        _order(21.101, 81.301, weight=5.0),
        _order(21.102, 81.302, weight=8.0),
    ]

    with patch(
        "logistics_service.pooling.get_route",
        new=AsyncMock(return_value=_MOCK_ROUTE),
    ):
        pools = await run_pooling(POOL_DATE, orders, session_factory, settings)

    assert len(pools) == 1
    async with session_factory() as session:
        result = await session.execute(
            select(PoolStop).where(PoolStop.pool_id == pools[0].id).order_by(PoolStop.seq)
        )
        stops = result.scalars().all()
        # 2 orders -> 4 stops: PICKUP (seq 1), PICKUP (seq 2), DROP (seq 3), DROP (seq 4)
        assert len(stops) == 4
        assert [s.seq for s in stops] == [1, 2, 3, 4]
        assert stops[0].stop_type == "PICKUP"
        assert stops[1].stop_type == "PICKUP"
        assert stops[2].stop_type == "DROP"
        assert stops[3].stop_type == "DROP"
