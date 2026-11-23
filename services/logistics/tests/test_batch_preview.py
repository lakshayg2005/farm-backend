"""GET /pools/batch (admin preview of the shared-delivery batch) and the mode on POST /pools/run."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient


def _order(lat: float, lng: float, weight: float = 10.0, due: str = "2000-01-01") -> dict:
    return {
        "order_id": str(uuid4()),
        "delivery_mode": "FLEXIBLE",
        "delivery_date": due,
        "hub_lat": 21.1,
        "hub_lng": 81.3,
        "drop_lat": lat,
        "drop_lng": lng,
        "weight_kg": weight,
        "address": f"{lat},{lng}",
    }


@pytest.mark.asyncio
async def test_preview_groups_waiting_orders_by_neighbourhood(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    waiting = [
        _order(21.10, 81.30),
        _order(21.11, 81.31),
        _order(21.12, 81.30),  # one neighbourhood
        _order(21.60, 81.80),  # another, far away
        _order(21.10, 81.30, due="2999-01-01"),  # not due at the next run
    ]
    with patch(
        "logistics_service.routes.fetch_ready_orders", new=AsyncMock(return_value=waiting)
    ) as fetch:
        response = await client.get("/pools/batch", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert fetch.await_args.args[2] == "FLEXIBLE"
    body = response.json()
    assert body["waitingOrders"] == 5
    assert body["dueOrders"] == 4
    assert body["estimatedRoutes"] == 2
    assert body["totalWeightKg"] == 40.0
    routes = sorted({o["route"] for o in body["orders"]})
    assert routes == [1, 2]
    assert body["batchTime"] == "10:00"


@pytest.mark.asyncio
async def test_preview_is_admin_only(client: AsyncClient, delivery_headers: dict[str, str]) -> None:
    response = await client.get("/pools/batch", headers=delivery_headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_run_with_mode_flexible_runs_the_batch(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    with (
        patch(
            "logistics_service.routes.fetch_ready_orders", new=AsyncMock(return_value=[])
        ) as fetch,
        patch("logistics_service.routes.run_pooling", new=AsyncMock(return_value=[])) as run,
    ):
        response = await client.post(
            "/pools/run", params={"mode": "FLEXIBLE"}, headers=admin_headers
        )
    assert response.status_code == 202, response.text
    assert fetch.await_args.args[2] == "FLEXIBLE"
    assert run.await_args.kwargs == {"batch": True}


@pytest.mark.asyncio
async def test_orders_already_on_a_route_are_not_waiting_for_the_batch(
    client: AsyncClient, admin_headers: dict[str, str], session_factory
) -> None:
    """A pool made by an earlier run may still be offered to partners; its orders stay
    READY_FOR_DISPATCH in the order service until one accepts, but they are not waiting any more."""
    from datetime import date
    from unittest.mock import AsyncMock as AM

    from logistics_service.pooling import run_pooling
    from logistics_service.settings import LogisticsSettings

    on_route, still_waiting = _order(21.10, 81.30), _order(21.60, 81.80)
    with patch("logistics_service.pooling.get_route", new=AM(return_value=None)):
        await run_pooling(
            date(2026, 11, 24),
            [on_route],
            session_factory,
            LogisticsSettings(events_enabled=False, dispatch_mode="assign"),
            batch=True,
        )
    with patch(
        "logistics_service.routes.fetch_ready_orders",
        new=AsyncMock(return_value=[on_route, still_waiting]),
    ):
        body = (await client.get("/pools/batch", headers=admin_headers)).json()

    assert body["waitingOrders"] == 1
    assert [o["orderId"] for o in body["orders"]] == [still_waiting["order_id"]]
