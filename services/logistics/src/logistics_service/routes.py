"""Public API routes for the logistics service (HLD section 15).

Mounted at /api/v1/logistics by the gateway.

POST  /logistics/pools/run?date&mode Admin → trigger pooling job now (mode=FLEXIBLE: the batch)
GET   /logistics/pools/batch        Admin → the shared-delivery batch waiting for its next run
GET   /logistics/pools?pool_date    Admin → saved pools for a date
GET   /logistics/my-route?date      Delivery partner → today's assigned route
POST  /logistics/stops/{id}/pickup  Delivery partner → confirm pickup at hub
POST  /logistics/stops/{id}/deliver Delivery partner → confirm delivery + photo + OTP
POST  /logistics/stops/{id}/arrive  Delivery partner → mark arrival at stop
POST  /logistics/stops/{id}/skip    Delivery partner → mark stop skipped
POST  /logistics/location           Delivery partner → location ping (every 30 s)
GET   /logistics/orders/{id}/track  Customer → live tracking
GET   /logistics/offers/mine        Delivery partner → open route offers
POST  /logistics/offers/{pool}/accept   Delivery partner → take the route (first wins)
POST  /logistics/offers/{pool}/decline  Delivery partner → turn the offer down
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.auth import CurrentUser, Role, get_current_user, require_roles
from farm_common.errors import AppError, NotFound
from farm_common.events import Event
from farm_common.outbox import add_event
from logistics_service.clustering import cluster_orders
from logistics_service.database import get_session
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
from logistics_service.pooling import IST, fetch_ready_orders, india_today, run_pooling
from logistics_service.schemas import (
    BatchOrderOut,
    BatchPreview,
    DeliverIn,
    LocationPingIn,
    OfferOut,
    OfferPlace,
    OfferStop,
    PickupIn,
    PoolOut,
    PoolRunResponse,
    StopActionIn,
    StopOut,
    TrackOut,
)
from logistics_service.schemas import PoolStatus as SchemaPoolStatus
from logistics_service.schemas import StopType as SchemaStopType
from logistics_service.settings import LogisticsSettings, get_logistics_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["logistics"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stop_out(s: PoolStop, show_otp: bool = False) -> StopOut:
    """A stop as the API shows it. The delivery code (`otp`) is shown only to the order's customer
    (show_otp=True in the tracking route); partners and admins never see it."""
    from logistics_service.schemas import StopStatus as SStatus
    from logistics_service.schemas import StopType as SType

    return StopOut(
        id=s.id,
        pool_id=s.pool_id,
        order_id=s.order_id,
        stop_type=SType(s.stop_type),
        seq=s.seq,
        lat=s.lat,
        lng=s.lng,
        address=s.address,
        eta=s.eta,
        status=SStatus(s.status),
        photo_media_id=s.photo_media_id,
        otp_verified=s.otp_verified,
        done_at=s.done_at,
        weight_kg=s.weight_kg,
        customer_name=s.customer_name,
        customer_phone=s.customer_phone,
        notes=s.notes,
        otp=s.otp if show_otp else None,
    )


async def _get_stop(
    session: AsyncSession, stop_id: UUID, user: CurrentUser | None = None
) -> PoolStop:
    stop = await session.get(PoolStop, stop_id)
    if stop is None:
        raise NotFound("Stop not found", code="STOP_NOT_FOUND")
    if user is not None and not user.has_any(Role.ADMIN):
        pool = await session.get(DeliveryPool, stop.pool_id)
        if pool is not None and pool.partner_id is not None and pool.partner_id != UUID(user.id):
            raise AppError(
                "POOL_ASSIGNED_TO_OTHER", "This pool is assigned to another delivery partner", 403
            )
    return stop


async def _get_pool_with_stops(
    session: AsyncSession, pool_id: UUID
) -> tuple[DeliveryPool, list[PoolStop]]:
    pool = await session.get(DeliveryPool, pool_id)
    if pool is None:
        raise NotFound("Pool not found", code="POOL_NOT_FOUND")
    result = await session.execute(
        select(PoolStop).where(PoolStop.pool_id == pool_id).order_by(PoolStop.seq)
    )
    stops = list(result.scalars())
    return pool, stops


def _road_path(pool: DeliveryPool) -> list[tuple[float, float]] | None:
    """The stored road line as [lat, lng] pairs, if it was computed through the real stop order."""
    if pool.geom is None or not (pool.osrm_meta or {}).get("stops_in_order"):
        return None
    return [(lat, lng) for lng, lat in to_shape(pool.geom).coords]


def _pool_out(pool: DeliveryPool, stops: list[PoolStop], with_path: bool = False) -> PoolOut:
    return PoolOut(
        id=pool.id,
        pool_date=pool.pool_date,
        partner_id=pool.partner_id,
        status=SchemaPoolStatus(pool.status),
        total_km=pool.total_km,
        km_saved=pool.km_saved,
        stops=[_stop_out(s) for s in stops],
        route_path=_road_path(pool) if with_path else None,
        created_at=pool.created_at,
        assigned_at=pool.assigned_at,
    )


# ---------------------------------------------------------------------------
# POST /logistics/pools/run  (Admin only)
# ---------------------------------------------------------------------------


@router.post(
    "/pools/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PoolRunResponse,
    summary="Run the pooling job for a given date",
)
async def run_pools(
    pool_date: date = Query(
        default_factory=india_today, description="Date to pool orders for (default: today in India)"
    ),
    mode: Literal["IMMEDIATE", "FLEXIBLE"] | None = Query(
        default=None,
        description="Pool only IMMEDIATE or only FLEXIBLE (the shared-delivery batch) orders; "
        "both when omitted",
    ),
    _user: CurrentUser = Depends(require_roles(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
    settings: LogisticsSettings = Depends(get_logistics_settings),
) -> PoolRunResponse:
    """Fetch READY_FOR_DISPATCH orders, cluster them, persist pools, publish pool.assigned.

    With mode=FLEXIBLE this is the daily shared-delivery batch run by hand ("run the batch now").
    """
    from logistics_service.database import get_session_factory

    orders = await fetch_ready_orders(pool_date, settings.order_service_url, mode)
    pools = await run_pooling(
        pool_date, orders, get_session_factory(), settings, batch=mode == "FLEXIBLE"
    )

    # Reload with stops for the response
    pool_outs: list[PoolOut] = []
    for p in pools:
        _, stops = await _get_pool_with_stops(session, p.id)
        pool_outs.append(_pool_out(p, stops))

    return PoolRunResponse(pools_created=len(pools), pools=pool_outs)


# ---------------------------------------------------------------------------
# GET /logistics/pools/batch  (Admin only)
# ---------------------------------------------------------------------------


@router.get(
    "/pools/batch",
    response_model=BatchPreview,
    summary="The shared-delivery batch waiting for its next run (preview, creates nothing)",
)
async def batch_preview(
    _user: CurrentUser = Depends(require_roles(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
    settings: LogisticsSettings = Depends(get_logistics_settings),
) -> BatchPreview:
    """FLEXIBLE orders that are packed and waiting, and how the next run would group them."""
    now = datetime.now(IST)
    next_run = now.replace(
        hour=settings.batch_hour_ist, minute=settings.batch_minute_ist, second=0, microsecond=0
    )
    if next_run <= now:
        next_run += timedelta(days=1)
    # everything waiting, whatever its date, then what the next run would take
    waiting = await fetch_ready_orders(date.max, settings.order_service_url, "FLEXIBLE")
    # a route made by an earlier run may still be waiting for a partner to accept it: the order
    # stays READY_FOR_DISPATCH until then, but it is no longer waiting for the batch
    routed = set(
        (
            await session.execute(
                select(PoolStop.order_id).where(
                    PoolStop.order_id.in_([UUID(str(o["order_id"])) for o in waiting])
                )
            )
        ).scalars()
    )
    waiting = [o for o in waiting if UUID(str(o["order_id"])) not in routed]
    due = [
        o
        for o in waiting
        if not o.get("delivery_date") or date.fromisoformat(o["delivery_date"]) <= next_run.date()
    ]
    clusters = cluster_orders(
        due,
        eps_km=settings.batch_dbscan_eps_km,
        capacity_kg=settings.partner_capacity_kg,
        max_orders=settings.max_drops_per_pool,
        hub_eps_km=settings.batch_hub_eps_km,
    )
    rows = [
        BatchOrderOut(
            order_id=UUID(str(o["order_id"])),
            address=o.get("address"),
            weight_kg=float(o.get("weight_kg") or 0),
            delivery_date=date.fromisoformat(o["delivery_date"])
            if o.get("delivery_date")
            else None,
            route=n,
        )
        for n, cluster in enumerate(clusters, start=1)
        for o in cluster
    ]
    return BatchPreview(
        batch_time=f"{settings.batch_hour_ist:02d}:{settings.batch_minute_ist:02d}",
        next_run_at=next_run,
        waiting_orders=len(waiting),
        due_orders=len(due),
        estimated_routes=len(clusters),
        total_weight_kg=round(sum(r.weight_kg for r in rows), 2),
        orders=rows,
    )


# ---------------------------------------------------------------------------
# GET /logistics/pools  (Admin only)
# ---------------------------------------------------------------------------


@router.get(
    "/pools",
    response_model=list[PoolOut],
    summary="Saved pools for a given date",
)
async def list_pools(
    pool_date: date = Query(
        default_factory=india_today, description="Date to list pools for (default: today in India)"
    ),
    _user: CurrentUser = Depends(require_roles(Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> list[PoolOut]:
    """Every pool for `pool_date` with its stops in `seq` order, oldest pool first."""
    pools = list(
        (
            await session.execute(
                select(DeliveryPool)
                .where(DeliveryPool.pool_date == pool_date)
                .order_by(DeliveryPool.created_at, DeliveryPool.id)
            )
        ).scalars()
    )
    if not pools:
        return []

    stops = (
        await session.execute(
            select(PoolStop)
            .where(PoolStop.pool_id.in_([p.id for p in pools]))
            .order_by(PoolStop.pool_id, PoolStop.seq)
        )
    ).scalars()
    stops_by_pool: dict[UUID, list[PoolStop]] = {p.id: [] for p in pools}
    for s in stops:
        stops_by_pool[s.pool_id].append(s)

    return [_pool_out(p, stops_by_pool[p.id]) for p in pools]


# ---------------------------------------------------------------------------
# GET /logistics/my-route  (Delivery partner)
# ---------------------------------------------------------------------------


@router.get(
    "/my-route",
    response_model=PoolOut,
    summary="Today's assigned pool for the authenticated delivery partner",
)
async def my_route(
    pool_date: date = Query(default_factory=india_today),
    user: CurrentUser = Depends(require_roles(Role.DELIVERY)),
    session: AsyncSession = Depends(get_session),
) -> PoolOut:
    result = await session.execute(
        select(DeliveryPool)
        .where(
            DeliveryPool.partner_id == UUID(user.id),
            DeliveryPool.pool_date == pool_date,
        )
        .order_by(DeliveryPool.created_at.desc())
        .limit(1)
    )
    pool = result.scalar_one_or_none()
    if pool is None:
        # Check if there is an active assigned / in-progress pool for this partner
        result = await session.execute(
            select(DeliveryPool)
            .where(
                DeliveryPool.partner_id == UUID(user.id),
                DeliveryPool.status.in_([PoolStatus.IN_PROGRESS, PoolStatus.ASSIGNED]),
            )
            .order_by(DeliveryPool.created_at.desc())
            .limit(1)
        )
        pool = result.scalar_one_or_none()

    if pool is None:
        raise NotFound("No pool assigned for this date", code="NO_POOL")

    _, stops = await _get_pool_with_stops(session, pool.id)
    return _pool_out(pool, stops, with_path=True)


# ---------------------------------------------------------------------------
# POST /logistics/stops/{id}/pickup  (Delivery partner)
# ---------------------------------------------------------------------------


@router.post(
    "/stops/{stop_id}/pickup",
    response_model=StopOut,
    summary="Mark a PICKUP stop as done",
)
async def confirm_pickup(
    stop_id: UUID,
    body: PickupIn | None = None,
    user: CurrentUser = Depends(require_roles(Role.DELIVERY)),
    session: AsyncSession = Depends(get_session),
) -> StopOut:
    pickup_body = body or PickupIn()
    async with session.begin():
        stop = await _get_stop(session, stop_id, user)
        if stop.stop_type != StopType.PICKUP:
            raise AppError("WRONG_STOP_TYPE", "This stop is not a PICKUP stop", 422)
        if stop.status == StopStatus.DONE:
            return _stop_out(stop)
        stop.status = StopStatus.DONE
        stop.done_at = pickup_body.collected_at or datetime.now(UTC)
        if pickup_body.weight_kg is not None:
            stop.weight_kg = pickup_body.weight_kg
        if pickup_body.notes:
            stop.notes = pickup_body.notes

        # Advance pool to IN_PROGRESS if currently PENDING or ASSIGNED
        pool = await session.get(DeliveryPool, stop.pool_id)
        if pool:
            if pool.status in (PoolStatus.PENDING, PoolStatus.ASSIGNED):
                pool.status = PoolStatus.IN_PROGRESS
            if not pool.partner_id:
                pool.partner_id = UUID(user.id)

        # Publish delivery.started event through outbox
        add_event(
            session,
            OutboxEvent,
            Event(
                type="delivery.started",
                producer="logistics",
                payload={
                    "poolId": str(stop.pool_id),
                    "stopId": str(stop.id),
                    "orderId": str(stop.order_id),
                    "orderIds": [str(stop.order_id)],
                    "partnerId": str(user.id),
                    "pickedUpAt": stop.done_at.isoformat(),
                },
            ),
        )
    return _stop_out(stop)


# ---------------------------------------------------------------------------
# POST /logistics/stops/{id}/deliver  (Delivery partner)
# ---------------------------------------------------------------------------


@router.post(
    "/stops/{stop_id}/deliver",
    response_model=StopOut,
    summary="Complete a DROP stop with photo proof + OTP",
)
async def confirm_delivery(
    stop_id: UUID,
    body: DeliverIn,
    user: CurrentUser = Depends(require_roles(Role.DELIVERY)),
    session: AsyncSession = Depends(get_session),
) -> StopOut:
    async with session.begin():
        stop = await _get_stop(session, stop_id, user)
        if stop.stop_type != StopType.DROP:
            raise AppError("WRONG_STOP_TYPE", "This stop is not a DROP stop", 422)
        if stop.status == StopStatus.DONE:
            return _stop_out(stop)
        waiting_pickup = await session.scalar(
            select(PoolStop.id).where(
                PoolStop.pool_id == stop.pool_id,
                PoolStop.order_id == stop.order_id,
                PoolStop.stop_type == StopType.PICKUP,
                PoolStop.status != StopStatus.DONE,
            )
        )
        if waiting_pickup is not None:
            raise AppError(
                "PICKUP_FIRST", "Collect this order at the farm before delivering it", 409
            )
        if not body.otp.isdigit():
            raise AppError("INVALID_OTP", "OTP must be 4 digits", 422)
        if stop.otp and not secrets.compare_digest(body.otp, stop.otp):
            raise AppError("INVALID_OTP", "That code does not match; ask the customer again", 422)

        stop.photo_media_id = body.photo_media_id
        stop.otp_verified = True
        stop.status = StopStatus.DONE
        stop.done_at = body.delivered_at or datetime.now(UTC)
        if body.notes:
            stop.notes = body.notes

        # Check if all stops in the pool are now DONE
        stops_result = await session.execute(
            select(PoolStop).where(PoolStop.pool_id == stop.pool_id)
        )
        all_stops = list(stops_result.scalars())
        closed = (StopStatus.DONE, StopStatus.SKIPPED)  # a cancelled order's stops are skipped
        if all(s.status in closed or s.id == stop.id for s in all_stops):
            pool = await session.get(DeliveryPool, stop.pool_id)
            if pool:
                pool.status = PoolStatus.COMPLETED

        # Publish delivery.completed event through outbox (Issue #20 requirement)
        add_event(
            session,
            OutboxEvent,
            Event(
                type="delivery.completed",
                producer="logistics",
                payload={
                    "poolId": str(stop.pool_id),
                    "stopId": str(stop.id),
                    "orderId": str(stop.order_id),
                    "orderIds": [str(stop.order_id)],
                    "partnerId": str(user.id),
                    "photoMediaId": str(body.photo_media_id),
                    "otpVerified": True,
                    "deliveredAt": stop.done_at.isoformat(),
                },
            ),
        )
    return _stop_out(stop)


# ---------------------------------------------------------------------------
# POST /logistics/stops/{id}/arrive  (Delivery partner)
# ---------------------------------------------------------------------------


@router.post(
    "/stops/{stop_id}/arrive",
    response_model=StopOut,
    summary="Mark partner arrival at a stop",
)
async def arrive_at_stop(
    stop_id: UUID,
    user: CurrentUser = Depends(require_roles(Role.DELIVERY)),
    session: AsyncSession = Depends(get_session),
) -> StopOut:
    async with session.begin():
        stop = await _get_stop(session, stop_id, user)
        if stop.status != StopStatus.DONE:
            stop.status = StopStatus.ARRIVED
    return _stop_out(stop)


# ---------------------------------------------------------------------------
# POST /logistics/stops/{id}/skip  (Delivery partner)
# ---------------------------------------------------------------------------


@router.post(
    "/stops/{stop_id}/skip",
    response_model=StopOut,
    summary="Mark a stop as skipped",
)
async def skip_stop(
    stop_id: UUID,
    body: StopActionIn | None = None,
    user: CurrentUser = Depends(require_roles(Role.DELIVERY)),
    session: AsyncSession = Depends(get_session),
) -> StopOut:
    action_body = body or StopActionIn()
    async with session.begin():
        stop = await _get_stop(session, stop_id, user)
        if stop.status == StopStatus.DONE:
            raise AppError("STOP_DONE", "This stop is already done", 409)
        stop.status = StopStatus.SKIPPED
        if action_body.notes or action_body.reason:
            stop.notes = action_body.notes or action_body.reason
    return _stop_out(stop)


# ---------------------------------------------------------------------------
# POST /logistics/location  (Delivery partner)
# ---------------------------------------------------------------------------


@router.post(
    "/location",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Record a GPS location ping (every 30 s while on route)",
)
async def record_location(
    body: LocationPingIn,
    user: CurrentUser = Depends(require_roles(Role.DELIVERY)),
    session: AsyncSession = Depends(get_session),
) -> None:
    pool_id = body.pool_id
    if pool_id is None:
        # Auto-resolve partner's active pool
        active_pool = await session.execute(
            select(DeliveryPool.id)
            .where(
                DeliveryPool.partner_id == UUID(user.id),
                DeliveryPool.status.in_([PoolStatus.IN_PROGRESS, PoolStatus.ASSIGNED]),
            )
            .order_by(DeliveryPool.created_at.desc())
            .limit(1)
        )
        pool_id = active_pool.scalar_one_or_none()

    # The lookup above already began a transaction, so commit rather than begin().
    session.add(
        LocationPing(
            partner_id=UUID(user.id),
            pool_id=pool_id,
            lat=body.lat,
            lng=body.lng,
            accuracy_m=body.accuracy_m,
            speed_ms=body.speed_ms,
            heading=body.heading,
            recorded_at=body.recorded_at or datetime.now(UTC),
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# GET /logistics/orders/{id}/track  (Customer)
# ---------------------------------------------------------------------------


@router.get(
    "/orders/{order_id}/track",
    response_model=TrackOut,
    summary="Live tracking for a customer order",
)
async def track_order(
    order_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TrackOut:
    """Where the order is, who brings it and the delivery code. Only the customer of the order
    (and an admin) may see this; anyone else gets 404, as if the order had no route."""
    # Find the active DROP stop for this order
    result = await session.execute(
        select(PoolStop)
        .where(
            PoolStop.order_id == order_id,
            PoolStop.stop_type == StopType.DROP,
        )
        .order_by(PoolStop.seq.desc())
        .limit(1)
    )
    stop = result.scalar_one_or_none()
    is_customer = stop is not None and str(stop.customer_id) == user.id
    if stop is not None and not (is_customer or user.has_any(Role.ADMIN)):
        raise NotFound("No delivery for this order", code="ORDER_NOT_TRACKED")

    partner_id = None
    partner_name = partner_phone = None
    partner_lat = partner_lng = None
    partner_updated_at = None
    pool_id = stop.pool_id if stop else None
    order_status = stop.status if stop else None
    eta = stop.eta if stop else None

    if pool_id:
        pool = await session.get(DeliveryPool, pool_id)
        if pool:
            partner_id = pool.partner_id
            if pool.partner_id:
                partner = await session.get(DeliveryPartner, pool.partner_id)
                if partner:
                    partner_name = partner.name
                    partner_phone = partner.phone
                ping_result = await session.execute(
                    select(LocationPing)
                    .where(LocationPing.partner_id == pool.partner_id)
                    .order_by(LocationPing.recorded_at.desc())
                    .limit(1)
                )
                ping = ping_result.scalar_one_or_none()
                if ping:
                    partner_lat = ping.lat
                    partner_lng = ping.lng
                    partner_updated_at = ping.recorded_at

            if stop and stop.status == StopStatus.DONE:
                order_status = "DELIVERED"
            elif pool.status == PoolStatus.IN_PROGRESS:
                order_status = "IN_TRANSIT"
            else:
                order_status = "POOLED"

    return TrackOut(
        order_id=order_id,
        pool_id=pool_id,
        status=order_status,
        stop=_stop_out(stop, show_otp=is_customer) if stop else None,
        partner_id=partner_id,
        partner_name=partner_name,
        partner_phone=partner_phone,
        partner_lat=partner_lat,
        partner_lng=partner_lng,
        partner_updated_at=partner_updated_at,
        eta=eta,
    )


# ---------------------------------------------------------------------------
# Route offers (dispatch.py): a new pool is offered to nearby partners, first to accept gets it
# ---------------------------------------------------------------------------


@router.get(
    "/offers/mine",
    response_model=list[OfferOut],
    summary="Open route offers for the signed-in delivery partner",
)
async def my_offers(
    user: CurrentUser = Depends(require_roles(Role.DELIVERY)),
    session: AsyncSession = Depends(get_session),
) -> list[OfferOut]:
    from logistics_service.dispatch import open_offers

    out: list[OfferOut] = []
    for offer, pool, stops in await open_offers(session, UUID(user.id)):
        pickups = [s for s in stops if s.stop_type == StopType.PICKUP]
        first = pickups[0] if pickups else None
        out.append(
            OfferOut(
                pool_id=pool.id,
                pool_date=pool.pool_date,
                stop_count=len(stops),
                pickup_count=len(pickups),
                drop_count=len(stops) - len(pickups),
                total_km=pool.total_km,
                distance_km=offer.distance_km,
                first_pickup=OfferPlace(lat=first.lat, lng=first.lng, address=first.address)
                if first
                else None,
                stops=[
                    OfferStop(
                        seq=s.seq, stop_type=SchemaStopType(s.stop_type), lat=s.lat, lng=s.lng
                    )
                    for s in stops
                ],
                offered_at=offer.created_at,
                expires_at=offer.expires_at,
            )
        )
    return out


@router.post(
    "/offers/{pool_id}/accept",
    response_model=PoolOut,
    summary="Take an offered route; the first partner to accept gets it",
    responses={409: {"description": "POOL_TAKEN: another partner accepted first"}},
)
async def accept_route_offer(
    pool_id: UUID,
    user: CurrentUser = Depends(require_roles(Role.DELIVERY)),
    session: AsyncSession = Depends(get_session),
) -> PoolOut:
    from logistics_service.dispatch import accept_offer

    async with session.begin():
        pool = await accept_offer(session, pool_id, UUID(user.id))
    _, stops = await _get_pool_with_stops(session, pool.id)
    return _pool_out(pool, stops, with_path=True)


@router.post(
    "/offers/{pool_id}/decline",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Turn an offered route down",
)
async def decline_route_offer(
    pool_id: UUID,
    user: CurrentUser = Depends(require_roles(Role.DELIVERY)),
    session: AsyncSession = Depends(get_session),
) -> None:
    from logistics_service.dispatch import decline_offer

    async with session.begin():
        await decline_offer(session, pool_id, UUID(user.id))
