"""Route offers: who is offered a new pool, first-to-accept wins, the trigger after packing."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from logistics_service.consumers import PoolTrigger, delivery_date_of
from logistics_service.db import (
    DeliveryPartner,
    DeliveryPool,
    LocationPing,
    OfferStatus,
    OutboxEvent,
    PoolOffer,
    PoolStatus,
)
from logistics_service.dispatch import Candidate, pick_offer_targets
from logistics_service.pooling import run_pooling
from logistics_service.routing import RouteResult
from logistics_service.settings import LogisticsSettings

PICKUP = (21.0, 81.0)

# ---------------------------------------------------------------------------
# Pure: who gets the offer
# ---------------------------------------------------------------------------


def test_nearby_online_partners_nearest_first() -> None:
    a, b, far, off = uuid4(), uuid4(), uuid4(), uuid4()
    targets = pick_offer_targets(
        [
            Candidate(b, (21.05, 81.0), True),  # ~5.6 km
            Candidate(a, (21.01, 81.0), True),  # ~1.1 km
            Candidate(far, (21.5, 81.5), True),  # ~75 km
            Candidate(off, (21.0, 81.0), False),  # right there but offline
        ],
        PICKUP,
        radius_km=15,
    )
    assert [t[0] for t in targets] == [a, b]
    assert targets[0][1] == pytest.approx(1.1, abs=0.1)


def test_nobody_close_widens_to_every_online_partner() -> None:
    far, off = uuid4(), uuid4()
    targets = pick_offer_targets(
        [Candidate(far, (21.5, 81.5), True), Candidate(off, None, False)], PICKUP, 15
    )
    assert [t[0] for t in targets] == [far]


def test_nobody_online_offers_every_known_partner() -> None:
    a, b = uuid4(), uuid4()
    targets = pick_offer_targets(
        [Candidate(a, None, False), Candidate(b, (21.2, 81.0), False)], PICKUP, 15
    )
    assert [t[0] for t in targets] == [b, a]  # known position first, unknown last
    assert pick_offer_targets([], PICKUP, 15) == []


# ---------------------------------------------------------------------------
# Pure: the trigger after an order is packed
# ---------------------------------------------------------------------------


def test_pool_date_is_the_delivery_date_never_in_the_past() -> None:
    today = date(2026, 11, 23)
    assert delivery_date_of({"deliveryDate": "2026-11-25"}, today) == date(2026, 11, 25)
    assert delivery_date_of({"deliveryDate": "2026-11-20"}, today) == today
    assert delivery_date_of({}, today) == today
    assert delivery_date_of({"deliveryDate": "garbage"}, today) == today


@pytest.mark.asyncio
async def test_orders_packed_together_share_one_pooling_run() -> None:
    runs: list[date] = []

    async def run(day: date) -> None:
        runs.append(day)

    trigger = PoolTrigger(run, delay_s=0.05)
    day = date(2026, 11, 23)
    trigger.request(day)
    trigger.request(day)
    trigger.request(date(2026, 11, 24))
    await asyncio.sleep(0.2)
    assert sorted(runs) == [day, date(2026, 11, 24)]
    trigger.request(day)  # a later burst runs again
    await asyncio.sleep(0.2)
    assert runs.count(day) == 2


# ---------------------------------------------------------------------------
# Database: offers are created, and exactly one of two simultaneous accepts wins
# ---------------------------------------------------------------------------

POOL_DATE = date(2026, 11, 17)
_ROUTE = RouteResult(
    total_km=5.0,
    km_saved=1.0,
    linestring_coords=[(21.0, 81.0), (21.1, 81.3)],
    waypoint_order=[0, 1],
    etas=[],
    osrm_meta={},
)


def _order() -> dict[str, Any]:
    return {
        "order_id": uuid4(),
        "hub_lat": 21.0,
        "hub_lng": 81.0,
        "drop_lat": 21.1,
        "drop_lng": 81.3,
        "weight_kg": 5.0,
        "address": "Test address",
    }


async def _offered_pool(
    session_factory: async_sessionmaker[AsyncSession], riders: int
) -> tuple[DeliveryPool, list]:
    ids = [uuid4() for _ in range(riders)]
    async with session_factory() as session, session.begin():
        for i, pid in enumerate(ids):
            session.add(DeliveryPartner(id=pid, name=f"Rider {i}"))
            session.add(
                LocationPing(
                    partner_id=pid, lat=21.0 + i * 0.01, lng=81.0, recorded_at=datetime.now(UTC)
                )
            )
    with patch("logistics_service.pooling.get_route", new=AsyncMock(return_value=_ROUTE)):
        pools = await run_pooling(
            POOL_DATE, [_order()], session_factory, LogisticsSettings(events_enabled=False)
        )
    return pools[0], ids


@pytest.mark.asyncio
async def test_new_pool_is_offered_not_assigned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pool, riders = await _offered_pool(session_factory, 2)
    assert pool.partner_id is None and pool.status == PoolStatus.PENDING
    async with session_factory() as session:
        offers = (
            (await session.execute(select(PoolOffer).where(PoolOffer.pool_id == pool.id)))
            .scalars()
            .all()
        )
        types = [e.type for e in (await session.execute(select(OutboxEvent))).scalars()]
    assert {o.partner_id for o in offers} == set(riders)
    assert all(o.status == OfferStatus.OFFERED and o.expires_at is not None for o in offers)
    assert "pool.offered" in types and "pool.assigned" not in types  # assigned only on accept


@pytest.mark.asyncio
async def test_two_riders_accept_at_once_exactly_one_wins(
    client: Any, session_factory: async_sessionmaker[AsyncSession], make_token: Any
) -> None:
    pool, (a, b) = await _offered_pool(session_factory, 2)
    ra, rb = await asyncio.gather(
        client.post(f"/offers/{pool.id}/accept", headers=make_token(a, "delivery")),
        client.post(f"/offers/{pool.id}/accept", headers=make_token(b, "delivery")),
    )
    codes = sorted([ra.status_code, rb.status_code])
    assert codes == [200, 409]
    loser = ra if ra.status_code == 409 else rb
    assert loser.json()["error"]["code"] == "POOL_TAKEN"
    winner = a if ra.status_code == 200 else b

    async with session_factory() as session:
        saved = await session.get(DeliveryPool, pool.id)
        offers = {
            o.partner_id: o.status for o in (await session.execute(select(PoolOffer))).scalars()
        }
        types = [e.type for e in (await session.execute(select(OutboxEvent))).scalars()]
    assert saved is not None and saved.partner_id == winner and saved.status == PoolStatus.ASSIGNED
    assert offers[winner] == OfferStatus.ACCEPTED
    assert types.count("pool.assigned") == 1 and "pool.offer_closed" in types

    # The loser's offer is gone from their list; the winner sees the route as theirs
    other = b if winner == a else a
    assert (await client.get("/offers/mine", headers=make_token(other, "delivery"))).json() == []
    again = await client.post(f"/offers/{pool.id}/accept", headers=make_token(winner, "delivery"))
    assert again.status_code == 200  # a retry by the winner is not an error


@pytest.mark.asyncio
async def test_open_offers_list_and_decline(
    client: Any, session_factory: async_sessionmaker[AsyncSession], make_token: Any
) -> None:
    pool, (a,) = await _offered_pool(session_factory, 1)
    mine = (await client.get("/offers/mine", headers=make_token(a, "delivery"))).json()
    assert len(mine) == 1 and mine[0]["poolId"] == str(pool.id) and mine[0]["stopCount"] == 2
    assert mine[0]["firstPickup"]["lat"] == 21.0
    route = mine[0]["stops"]
    assert [x["seq"] for x in route] == sorted(x["seq"] for x in route) and len(route) == 2
    assert route[0]["stopType"] == "PICKUP" and "address" not in route[0]
    assert (
        await client.post(f"/offers/{pool.id}/decline", headers=make_token(a, "delivery"))
    ).status_code == 204
    assert (await client.get("/offers/mine", headers=make_token(a, "delivery"))).json() == []
    stranger = await client.post(
        f"/offers/{pool.id}/accept", headers=make_token(uuid4(), "delivery")
    )
    assert stranger.status_code == 404
