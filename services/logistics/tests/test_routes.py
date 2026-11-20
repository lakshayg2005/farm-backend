"""API-level tests for the logistics routes (requires TEST_DATABASE_URL)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from logistics_service.db import (
    DeliveryPartner,
    DeliveryPool,
    LocationPing,
    OutboxEvent,
    PoolStatus,
    PoolStop,
    StopStatus,
    StopType,
)
from logistics_service.routing import RouteResult

_MOCK_ROUTE = RouteResult(
    total_km=8.0,
    km_saved=1.5,
    linestring_coords=[(21.0, 81.0), (21.1, 81.1)],
    waypoint_order=[0, 1],
    etas=[],
    osrm_meta={"fallback": True},
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /pools/run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pools_run_requires_admin(
    client: AsyncClient,
    delivery_headers: dict[str, str],
) -> None:
    resp = await client.post("/pools/run", headers=delivery_headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_pools_run_no_orders(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    with (
        patch("logistics_service.routes.fetch_ready_orders", new=AsyncMock(return_value=[])),
        patch(
            "logistics_service.routes.run_pooling",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.post("/pools/run?date=2026-11-17", headers=admin_headers)
    assert resp.status_code == 202
    body = resp.json()
    assert body["poolsCreated"] == 0
    assert body["pools"] == []


# ---------------------------------------------------------------------------
# GET /pools
# ---------------------------------------------------------------------------


async def _add_pool(
    session_factory: async_sessionmaker[AsyncSession], pool_date: date, n_stops: int
) -> DeliveryPool:
    pool = DeliveryPool(
        id=uuid4(),
        pool_date=pool_date,
        status=PoolStatus.PENDING,
        total_km=10.0,
        km_saved=2.0,
    )
    order_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(pool)
        await session.flush()
        # insert stops out of order to check the response sorts them by seq
        for seq in reversed(range(1, n_stops + 1)):
            session.add(
                PoolStop(
                    id=uuid4(),
                    pool_id=pool.id,
                    order_id=order_id,
                    stop_type=StopType.PICKUP if seq == 1 else StopType.DROP,
                    seq=seq,
                    lat=21.0 + seq / 100,
                    lng=81.0,
                    status=StopStatus.PENDING,
                )
            )
    return pool


@pytest.mark.asyncio
async def test_list_pools_returns_only_that_date(
    client: AsyncClient,
    admin_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    day = date(2026, 11, 18)
    first = await _add_pool(session_factory, day, 3)
    second = await _add_pool(session_factory, day, 2)
    await _add_pool(session_factory, date(2026, 11, 19), 2)

    resp = await client.get(f"/pools?pool_date={day.isoformat()}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert {p["id"] for p in body} == {str(first.id), str(second.id)}
    assert all(p["poolDate"] == day.isoformat() for p in body)
    by_id = {p["id"]: p for p in body}
    assert [s["seq"] for s in by_id[str(first.id)]["stops"]] == [1, 2, 3]
    assert [s["seq"] for s in by_id[str(second.id)]["stops"]] == [1, 2]
    assert by_id[str(first.id)]["kmSaved"] == 2.0


@pytest.mark.asyncio
async def test_list_pools_empty_date(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    resp = await client.get("/pools?pool_date=2030-01-01", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_pools_requires_admin(
    client: AsyncClient,
    make_token,
) -> None:
    resp = await client.get("/pools", headers=make_token(uuid4(), "customer"))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /location
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_location_ping(
    client: AsyncClient,
    delivery_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resp = await client.post(
        "/location",
        json={"lat": 21.1, "lng": 81.3, "accuracyM": 10.0, "speedMs": 5.5},
        headers=delivery_headers,
    )
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# GET /orders/{id}/track
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_track_requires_auth(client: AsyncClient) -> None:
    resp = await client.get(f"/orders/{uuid4()}/track")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_track_unknown_order(
    client: AsyncClient,
    delivery_headers: dict[str, str],
) -> None:
    resp = await client.get(f"/orders/{uuid4()}/track", headers=delivery_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stop"] is None
    assert body["poolId"] is None


# ---------------------------------------------------------------------------
# GET /my-route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_my_route_no_pool(
    client: AsyncClient,
    delivery_headers: dict[str, str],
) -> None:
    resp = await client.get("/my-route?date=2026-11-17", headers=delivery_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_my_route_with_assigned_pool(
    client: AsyncClient,
    make_token,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    partner_id = uuid4()
    pool_id = uuid4()
    order_id = uuid4()
    today = date(2026, 11, 18)

    async with session_factory() as session, session.begin():
        session.add(
            DeliveryPool(
                id=pool_id,
                pool_date=today,
                partner_id=partner_id,
                status=PoolStatus.ASSIGNED,
                total_km=12.5,
                km_saved=3.2,
            )
        )
        session.add(
            PoolStop(
                id=uuid4(),
                pool_id=pool_id,
                order_id=order_id,
                stop_type=StopType.PICKUP,
                seq=1,
                lat=21.1,
                lng=81.2,
                status=StopStatus.PENDING,
            )
        )
        session.add(
            PoolStop(
                id=uuid4(),
                pool_id=pool_id,
                order_id=order_id,
                stop_type=StopType.DROP,
                seq=2,
                lat=21.2,
                lng=81.3,
                status=StopStatus.PENDING,
                address="42 Green Park",
            )
        )

    headers = make_token(partner_id, "delivery")
    resp = await client.get(f"/my-route?date={today.isoformat()}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(pool_id)
    assert len(body["stops"]) == 2
    assert body["stops"][0]["stopType"] == "PICKUP"
    assert body["stops"][1]["stopType"] == "DROP"


# ---------------------------------------------------------------------------
# Stop actions & delivery.completed (Issue #20)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_pickup_and_deliver_flow(
    client: AsyncClient,
    make_token,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    partner_id = uuid4()
    pool_id = uuid4()
    order_id = uuid4()
    pickup_stop_id = uuid4()
    drop_stop_id = uuid4()
    today = date(2026, 11, 18)

    async with session_factory() as session, session.begin():
        session.add(
            DeliveryPool(
                id=pool_id,
                pool_date=today,
                partner_id=partner_id,
                status=PoolStatus.ASSIGNED,
            )
        )
        session.add(
            PoolStop(
                id=pickup_stop_id,
                pool_id=pool_id,
                order_id=order_id,
                stop_type=StopType.PICKUP,
                seq=1,
                lat=21.1,
                lng=81.2,
                status=StopStatus.PENDING,
            )
        )
        session.add(
            PoolStop(
                id=drop_stop_id,
                pool_id=pool_id,
                order_id=order_id,
                stop_type=StopType.DROP,
                seq=2,
                lat=21.2,
                lng=81.3,
                status=StopStatus.PENDING,
                otp="1234",
            )
        )

    headers = make_token(partner_id, "delivery")

    # 1. Pickup
    pickup_resp = await client.post(
        f"/stops/{pickup_stop_id}/pickup",
        json={"notes": "All crates loaded", "weightKg": 15.0},
        headers=headers,
    )
    assert pickup_resp.status_code == 200
    assert pickup_resp.json()["status"] == "DONE"

    # Verify delivery.started in outbox
    async with session_factory() as session:
        events = (
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.type == "delivery.started")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) >= 1
        assert events[-1].payload["orderId"] == str(order_id)

    # 2. Arrive at drop
    arrive_resp = await client.post(
        f"/stops/{drop_stop_id}/arrive",
        headers=headers,
    )
    assert arrive_resp.status_code == 200
    assert arrive_resp.json()["status"] == "ARRIVED"

    # 3. Deliver with photo and OTP -> publishes delivery.completed
    photo_id = uuid4()
    deliver_resp = await client.post(
        f"/stops/{drop_stop_id}/deliver",
        json={"photoMediaId": str(photo_id), "otp": "1234", "notes": "Handed to customer"},
        headers=headers,
    )
    assert deliver_resp.status_code == 200
    assert deliver_resp.json()["status"] == "DONE"
    assert deliver_resp.json()["otpVerified"] is True

    # Verify delivery.completed event published in outbox (Issue #20 requirement)
    async with session_factory() as session:
        completed_events = (
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.type == "delivery.completed")
                )
            )
            .scalars()
            .all()
        )
        assert len(completed_events) >= 1
        payload = completed_events[-1].payload
        assert payload["orderId"] == str(order_id)
        assert payload["poolId"] == str(pool_id)
        assert payload["photoMediaId"] == str(photo_id)
        assert payload["otpVerified"] is True

        # Pool should now be COMPLETED since all stops are done
        pool = await session.get(DeliveryPool, pool_id)
        assert pool is not None
        assert pool.status == PoolStatus.COMPLETED


@pytest.mark.asyncio
async def test_track_order_live_with_partner_location(
    client: AsyncClient,
    make_token,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    partner_id = uuid4()
    pool_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    drop_stop_id = uuid4()

    async with session_factory() as session, session.begin():
        session.add(
            DeliveryPartner(
                id=partner_id,
                name="Ramesh Kumar",
                phone="+919876543210",
                is_online=True,
            )
        )
        session.add(
            DeliveryPool(
                id=pool_id,
                pool_date=date(2026, 11, 18),
                partner_id=partner_id,
                status=PoolStatus.IN_PROGRESS,
            )
        )
        session.add(
            PoolStop(
                id=drop_stop_id,
                pool_id=pool_id,
                order_id=order_id,
                stop_type=StopType.DROP,
                seq=2,
                lat=21.2,
                lng=81.3,
                address="Sector 5, Durg",
                status=StopStatus.PENDING,
            )
        )
        session.add(
            LocationPing(
                partner_id=partner_id,
                pool_id=pool_id,
                lat=21.15,
                lng=81.25,
                accuracy_m=5.0,
                speed_ms=8.2,
                recorded_at=datetime.now(UTC),
            )
        )

    customer_headers = make_token(customer_id, "customer")
    track_resp = await client.get(f"/orders/{order_id}/track", headers=customer_headers)
    assert track_resp.status_code == 200
    data = track_resp.json()
    assert data["orderId"] == str(order_id)
    assert data["status"] == "IN_TRANSIT"
    assert data["partnerName"] == "Ramesh Kumar"
    assert data["partnerLat"] == pytest.approx(21.15)
    assert data["partnerLng"] == pytest.approx(81.25)
    assert data["stop"]["address"] == "Sector 5, Durg"


@pytest.mark.asyncio
async def test_only_the_assigned_partner_can_work_a_pool(
    client: AsyncClient,
    make_token,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner, other = uuid4(), uuid4()
    pool_id, stop_id = uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            DeliveryPool(
                id=pool_id,
                pool_date=date(2026, 11, 18),
                partner_id=owner,
                status=PoolStatus.ASSIGNED,
            )
        )
        session.add(
            PoolStop(
                id=stop_id,
                pool_id=pool_id,
                order_id=uuid4(),
                stop_type=StopType.PICKUP,
                seq=1,
                lat=21.1,
                lng=81.2,
                status=StopStatus.PENDING,
            )
        )

    denied = await client.post(
        f"/stops/{stop_id}/pickup", json={}, headers=make_token(other, "delivery")
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "POOL_ASSIGNED_TO_OTHER"
    ok = await client.post(
        f"/stops/{stop_id}/pickup", json={}, headers=make_token(owner, "delivery")
    )
    assert ok.status_code == 200
