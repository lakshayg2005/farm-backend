"""Core pooling engine: DBSCAN → OSRM → persist pools → publish pool.assigned.

Entry point: `run_pooling(pool_date, session_factory, settings)`.

Steps:
1. Fetch all orders ready for dispatch on `pool_date` (via internal order API
   or a denormalised copy in the logistics schema; for testability the caller
   passes a list of order dicts directly in the worker ctx).
2. Run DBSCAN clustering (eps ≈ 3 km, capacity-aware splitting).
3. For each cluster:
   a. Build waypoint list: hub first, then all pickups, then all drops
      (pickup-before-drop constraint).
   b. Call OSRM Trip API for optimised route + ETAs.
   c. Persist DeliveryPool + PoolStop rows.
   d. Publish `pool.assigned` via the outbox (same transaction).
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farm_common.events import Event
from farm_common.outbox import add_event
from logistics_service.clustering import cluster_orders
from logistics_service.db import (
    DeliveryPartner,
    DeliveryPool,
    OutboxEvent,
    PoolStatus,
    PoolStop,
    StopStatus,
    StopType,
)
from logistics_service.dispatch import offer_pool
from logistics_service.routing import RouteResult, get_road_path, get_route
from logistics_service.sequencing import PartnerCandidate, pick_partner, sequence_points
from logistics_service.settings import LogisticsSettings

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def india_today() -> date:
    """Delivery dates are India dates (the order service stores them that way)."""
    return datetime.now(IST).date()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

Order = dict[str, Any]  # order dict produced by the caller / order service


def _wkt_point(lat: float, lng: float) -> WKTElement:
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


def _wkt_linestring(coords: list[tuple[float, float]]) -> WKTElement | None:
    if len(coords) < 2:
        return None
    pairs = ", ".join(f"{lng} {lat}" for lat, lng in coords)
    return WKTElement(f"LINESTRING({pairs})", srid=4326)


def _pool_payload(pool: DeliveryPool, stops: list[PoolStop]) -> dict[str, Any]:
    return {
        "poolId": str(pool.id),
        "poolDate": pool.pool_date.isoformat(),
        "partnerId": str(pool.partner_id) if pool.partner_id else None,
        "totalKm": pool.total_km,
        "kmSaved": pool.km_saved,
        "stopCount": len(stops),
        "orderIds": list({str(s.order_id) for s in stops}),
    }


def stop_order(cluster: list[Order], route: RouteResult | None) -> tuple[list[Order], list[int]]:
    """The orders in drop order, and the indices (into that list) in pickup order.

    Drops follow OSRM's trip order when there is one, else nearest-neighbour + 2-opt from the hub.
    Every pickup comes before every drop.
    """
    hub = (cluster[0]["hub_lat"], cluster[0]["hub_lng"])
    ordered = [
        cluster[i] for i in sequence_points(hub, [(o["drop_lat"], o["drop_lng"]) for o in cluster])
    ]
    if route and route.waypoint_order:
        # waypoint_order contains original indices into [hub, *stops]; index 0 is the hub
        order_idx = [i - 1 for i in route.waypoint_order if i > 0]
        if len(order_idx) == len(cluster):
            ordered = [cluster[i] for i in order_idx]
    pickup_order = sequence_points(hub, [(o["hub_lat"], o["hub_lng"]) for o in ordered])
    return ordered, pickup_order


async def apply_road_path(
    route: RouteResult | None,
    cluster: list[Order],
    depart: datetime,
    osrm_base_url: str,
) -> RouteResult | None:
    """Replace the route's line, length and ETAs by the road path through the real stops.

    The trip call only sees hub -> drops, but the partner visits every pickup first, then every
    drop, so the line it returned does not match the stops. Here OSRM routes through the stops in
    exactly that order. On failure the route is returned unchanged.
    """
    if route is None or route.osrm_meta.get("fallback"):
        return route
    ordered, pickup_order = stop_order(cluster, route)
    points = [(ordered[i]["hub_lat"], ordered[i]["hub_lng"]) for i in pickup_order]
    points += [(o["drop_lat"], o["drop_lng"]) for o in ordered]
    result = await get_road_path(points, osrm_base_url=osrm_base_url)
    if result is None:
        return route
    path, total_km, legs = result

    # arrival time at every stop; the drops are the last len(ordered) points
    arrival = [depart]
    for sec in legs:
        arrival.append(arrival[-1] + timedelta(seconds=sec))
    first_drop = len(ordered)
    etas = {str(o["order_id"]): arrival[first_drop + j] for j, o in enumerate(ordered)}
    route.linestring_coords = path
    route.total_km = total_km
    route.drop_etas = etas
    route.osrm_meta = {**route.osrm_meta, "stops_in_order": True, "distance_m": total_km * 1000}
    return route


# ---------------------------------------------------------------------------
# Single-pool persistence
# ---------------------------------------------------------------------------


async def _persist_pool(
    session: AsyncSession,
    pool_date: date,
    cluster: list[Order],
    route: RouteResult | None,
    depart: datetime,
    partner_id: UUID | None = None,
    publish_assigned: bool = True,
) -> tuple[DeliveryPool, list[PoolStop]]:
    """Write one DeliveryPool + its PoolStop rows, publish pool.assigned.

    Pickup-before-drop ordering: all PICKUP stops get seq 1..k, all DROP
    stops get seq k+1..n; within each group we keep cluster order (which
    follows OSRM's waypoint_order when route is available).
    """
    pool_id = uuid4()

    # Build the route geometry
    # Only a road path through the real stop order is kept; the straight-line fallback is not
    # worth drawing (the app joins the stops itself).
    on_road = bool(route and route.osrm_meta.get("stops_in_order"))
    geom = _wkt_linestring(route.linestring_coords) if route and on_road else None
    total_km = route.total_km if route else None
    km_saved = route.km_saved if route else None
    osrm_meta = route.osrm_meta if route else None

    pool = DeliveryPool(
        id=pool_id,
        pool_date=pool_date,
        partner_id=partner_id,
        status=PoolStatus.ASSIGNED if partner_id else PoolStatus.PENDING,
        assigned_at=datetime.now(UTC) if partner_id else None,
        geom=geom,  # type: ignore[arg-type]
        total_km=total_km,
        km_saved=km_saved,
        osrm_meta=osrm_meta,
    )
    session.add(pool)

    ordered, pickup_order = stop_order(cluster, route)

    # ETA per order: the road path's leg times when we have them, else the trip's
    eta_by_order: dict[str, datetime] = {}
    if route and route.drop_etas is not None:
        eta_by_order = route.drop_etas
    elif route and route.etas:
        stop_indices = [i - 1 for i in route.waypoint_order if i > 0]
        for pos, orig_idx in enumerate(stop_indices):
            if pos < len(route.etas) and 0 <= orig_idx < len(cluster):
                eta_by_order[str(cluster[orig_idx]["order_id"])] = route.etas[pos]

    stops: list[PoolStop] = []
    seq = 1

    def _make_stop(order: Order, stype: StopType, s: int, eta: datetime | None) -> PoolStop:
        drop = stype == StopType.DROP
        lat = order["drop_lat"] if drop else order["hub_lat"]
        lng = order["drop_lng"] if drop else order["hub_lng"]
        customer = order.get("customer_id")
        return PoolStop(
            id=uuid4(),
            pool_id=pool_id,
            order_id=UUID(str(order["order_id"])),
            stop_type=stype.value,
            seq=s,
            lat=lat,
            lng=lng,
            geom=_wkt_point(lat, lng),  # type: ignore[arg-type]
            address=order.get("address"),
            eta=eta,
            status=StopStatus.PENDING,
            weight_kg=order.get("weight_kg"),
            customer_id=UUID(str(customer)) if customer else None,
            # the partner calls the customer at the door; nobody needs a phone for a farm pickup
            customer_phone=order.get("contact_phone") if drop else None,
            # proof of delivery: the customer reads this code to the partner (only they see it)
            otp=f"{secrets.randbelow(10_000):04d}" if drop else None,
        )

    # We model each order as 2 stops: PICKUP at its farm/hub, DROP at the customer. All pickups
    # come first (each drop after its pickup), visiting the farms in a short order too.
    for i in pickup_order:
        pickup = _make_stop(ordered[i], StopType.PICKUP, seq, None)
        session.add(pickup)
        stops.append(pickup)
        seq += 1

    for order in ordered:
        drop = _make_stop(order, StopType.DROP, seq, eta_by_order.get(str(order["order_id"])))
        session.add(drop)
        stops.append(drop)
        seq += 1

    if not publish_assigned:
        return pool, stops  # offered to partners first; pool.assigned goes out on accept
    # Publish pool.assigned (transactional outbox)
    add_event(
        session,
        OutboxEvent,
        Event(
            type="pool.assigned",
            producer="logistics",
            payload=_pool_payload(pool, stops),
        ),
    )
    return pool, stops


async def _choose_partner(
    session: AsyncSession, pool_date: date, first_pickup: tuple[float, float]
) -> UUID | None:
    """The least-loaded known delivery partner for the day, nearest the first pickup on a tie."""
    partners = (await session.execute(select(DeliveryPartner))).scalars().all()
    if not partners:
        return None  # nobody known yet: the first partner to confirm a pickup takes the pool
    loads = dict(
        (
            await session.execute(
                select(DeliveryPool.partner_id, func.count())
                .where(DeliveryPool.pool_date == pool_date, DeliveryPool.partner_id.is_not(None))
                .group_by(DeliveryPool.partner_id)
            )
        ).all()
    )
    chosen = pick_partner(
        [
            PartnerCandidate(
                id=p.id,
                name=p.name,
                pools_today=int(loads.get(p.id, 0)),
                home=(p.home_lat, p.home_lng)
                if p.home_lat is not None and p.home_lng is not None
                else None,
            )
            for p in partners
        ],
        first_pickup,
    )
    return chosen.id if chosen else None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_pooling(
    pool_date: date,
    orders: list[Order],
    session_factory: async_sessionmaker[AsyncSession],
    settings: LogisticsSettings,
    batch: bool = False,
) -> list[DeliveryPool]:
    """Run the full pooling job for `pool_date`.

    `batch=True` is the daily shared-delivery run (FLEXIBLE orders): a wider cluster radius, and
    pickups must be close too (see `batch_*` in the settings).

    `orders` is a list of dicts with keys:
        order_id, hub_lat, hub_lng, drop_lat, drop_lng,
        weight_kg (float), address (str | None)

    Returns the created DeliveryPool objects.
    """
    if not orders:
        logger.info("No orders to pool on %s", pool_date)
        return []
    # One run at a time (the admin button and the nightly job may meet), and an order that is
    # already on a route (its pool.assigned is still on the way to the order service) is skipped.
    async with session_factory() as lock:
        await lock.execute(select(func.pg_advisory_lock(POOLING_LOCK_KEY)))
        try:
            return await _pool(pool_date, orders, session_factory, settings, batch)
        finally:
            await lock.execute(select(func.pg_advisory_unlock(POOLING_LOCK_KEY)))


POOLING_LOCK_KEY = 7_202_611  # any constant; names the "pooling run" advisory lock


async def _pool(
    pool_date: date,
    orders: list[Order],
    session_factory: async_sessionmaker[AsyncSession],
    settings: LogisticsSettings,
    batch: bool = False,
) -> list[DeliveryPool]:
    async with session_factory() as session:
        on_a_route = set(
            (
                await session.execute(
                    select(PoolStop.order_id).where(
                        PoolStop.order_id.in_([UUID(str(o["order_id"])) for o in orders])
                    )
                )
            ).scalars()
        )
    orders = [o for o in orders if UUID(str(o["order_id"])) not in on_a_route]
    if not orders:
        logger.info("Every order due on %s is already on a route", pool_date)
        return []

    pools_list = cluster_orders(
        orders,
        eps_km=settings.batch_dbscan_eps_km if batch else settings.dbscan_eps_km,
        capacity_kg=settings.partner_capacity_kg,
        max_orders=settings.max_drops_per_pool,
        hub_eps_km=settings.batch_hub_eps_km if batch else None,
    )
    logger.info("Clustered %d orders into %d pools on %s", len(orders), len(pools_list), pool_date)

    if batch:
        # partners accept and reach the farms soon after the batch run
        depart = datetime.now(UTC) + timedelta(minutes=30)
    else:
        depart = datetime.now(UTC).replace(hour=6, minute=0, second=0, microsecond=0)
    created: list[DeliveryPool] = []

    for cluster in pools_list:
        # Waypoints: hub first (use hub of first order), then drop locations
        hub_lat = cluster[0]["hub_lat"]
        hub_lng = cluster[0]["hub_lng"]
        waypoints = [(hub_lat, hub_lng)] + [(o["drop_lat"], o["drop_lng"]) for o in cluster]

        route = await get_route(
            waypoints,
            depart_at=depart,
            osrm_base_url=settings.osrm_base_url,
        )
        route = await apply_road_path(route, cluster, depart, settings.osrm_base_url)

        async with session_factory() as session, session.begin():
            if settings.dispatch_mode == "offer":
                # Offer it to nearby partners; the first to accept gets it (dispatch.py)
                pool, stops = await _persist_pool(
                    session, pool_date, cluster, route, depart, None, publish_assigned=False
                )
                await session.flush()
                if not await offer_pool(session, pool, stops, settings):
                    # Nobody to offer it to: publish it unassigned, as before (the first partner
                    # to confirm a pickup takes it)
                    add_event(
                        session,
                        OutboxEvent,
                        Event(
                            type="pool.assigned",
                            producer="logistics",
                            payload=_pool_payload(pool, stops),
                        ),
                    )
            else:
                partner = await _choose_partner(session, pool_date, (hub_lat, hub_lng))
                pool, _stops = await _persist_pool(
                    session, pool_date, cluster, route, depart, partner
                )
        created.append(pool)

    logger.info("Created %d pools for %s", len(created), pool_date)
    return created


# ---------------------------------------------------------------------------
# Fetch pending orders (called from routes when no external data is passed)
# ---------------------------------------------------------------------------


async def fetch_ready_orders(
    pool_date: date,
    order_service_url: str,
    mode: str | None = None,
) -> list[Order]:
    """Fetch orders in READY_FOR_DISPATCH status from the order service.

    `mode` (IMMEDIATE or FLEXIBLE) limits it to one delivery mode; None returns both.

    Returns an empty list when the service is unavailable – the pooling job
    will then create zero pools (safe, can be re-run on demand).
    """
    import httpx

    url = f"{order_service_url}/internal/orders/ready-for-dispatch"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"date": pool_date.isoformat()}
            if mode:
                params["mode"] = mode
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("Could not fetch orders from %s: %s", url, exc)
        return []
