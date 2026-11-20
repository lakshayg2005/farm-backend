from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Response
from geoalchemy2 import Geography, Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.auth import CurrentUser, Role, require_roles
from farm_common.errors import AppError
from farm_common.events import Event
from farm_common.outbox import add_event
from land_service.agreements import build_terms
from land_service.database import get_session
from land_service.db import Agreement, CropUpdate, OutboxEvent, Plot, PlotSeason, Reservation
from land_service.geo import geometry_json, point_to_db, polygon_to_db
from land_service.schemas import (
    AgreementOut,
    CropUpdateCreate,
    PlotCreate,
    PlotOut,
    PlotUpdate,
    ReservationCreate,
    ReservationOut,
    SeasonCreate,
    SeasonOut,
)
from land_service.settings import get_land_settings

router = APIRouter()
farmer = require_roles(Role.FARMER)
farmer_admin = require_roles(Role.FARMER, Role.ADMIN)
customer = require_roles(Role.CUSTOMER)
LIVE = ("PENDING", "ACCEPTED", "ACTIVE", "HARVEST_READY")


def plot_out(row, boundary, centroid):
    return PlotOut(
        id=row.id,
        farmer_id=row.farmer_id,
        title=row.title,
        description=row.description,
        area_acres=float(row.area_acres),
        soil_type=row.soil_type,
        water_source=row.water_source,
        status=row.status,
        boundary=geometry_json(boundary),
        centroid=geometry_json(centroid),
        created_at=row.created_at,
    )


def season_out(row):
    return SeasonOut(
        id=row.id,
        plot_id=row.plot_id,
        season=row.season,
        start_date=row.start_date,
        end_date=row.end_date,
        price=float(row.price),
        allowed_crops=row.allowed_crops or [],
    )


def reservation_out(row):
    return ReservationOut.model_validate(
        {
            "id": row.id,
            "plot_season_id": row.plot_season_id,
            "customer_id": row.customer_id,
            "farmer_id": row.farmer_id,
            "crop_id": row.crop_id,
            "expected_qty_kg": row.expected_qty_kg,
            "deposit": row.deposit,
            "total": row.total,
            "status": row.status,
            "expires_at": row.expires_at,
        }
    )


async def load_plot(session, plot_id):
    row = await session.get(Plot, plot_id)
    if row is None or row.status == "DELETED":
        raise AppError("PLOT_NOT_FOUND", "Plot not found", 404)
    boundary, centroid = (
        await session.execute(select(Plot.boundary, Plot.centroid).where(Plot.id == plot_id))
    ).one()
    return plot_out(row, boundary, centroid)


@router.post("/plots", status_code=201, response_model=PlotOut, tags=["plots"])
async def create_plot(
    body: PlotCreate,
    user: CurrentUser = Depends(farmer),
    session: AsyncSession = Depends(get_session),
):
    boundary = polygon_to_db(body.boundary)
    typed_boundary = cast(boundary, Geometry(geometry_type="POLYGON", srid=4326))
    valid = await session.scalar(select(func.ST_IsValid(typed_boundary)))
    if not valid:
        raise AppError("INVALID_PLOT_BOUNDARY", "PostGIS rejected the plot polygon", 422)
    overlap = await session.scalar(
        select(Plot.id)
        .where(Plot.status == "ACTIVE", func.ST_Intersects(Plot.boundary, typed_boundary))
        .limit(1)
    )
    if overlap:
        raise AppError(
            "PLOT_OVERLAP",
            "Plot boundary intersects an existing active plot",
            409,
            details={"plotId": str(overlap)},
        )
    area = await session.scalar(
        select(func.ST_Area(cast(typed_boundary, Geography(srid=4326))) / 4046.8564224)
    )
    centroid = await session.scalar(
        select(func.ST_Centroid(typed_boundary, type_=Geometry(geometry_type="POINT", srid=4326)))
    )
    row = Plot(
        farmer_id=UUID(user.id),
        title=body.title,
        description=body.description,
        soil_type=body.soil_type,
        water_source=body.water_source,
        area_acres=area,
        boundary=boundary,
        centroid=centroid,
        status="ACTIVE",
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    result = plot_out(row, row.boundary, row.centroid)
    await session.commit()
    return result


@router.get("/plots/mine", response_model=list[PlotOut], tags=["plots"])
async def my_plots(
    user: CurrentUser = Depends(farmer), session: AsyncSession = Depends(get_session)
):
    rows = (
        (
            await session.execute(
                select(Plot)
                .where(Plot.farmer_id == UUID(user.id), Plot.status == "ACTIVE")
                .order_by(Plot.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [plot_out(r, r.boundary, r.centroid) for r in rows]


@router.get("/plots/search", tags=["search"])
async def search_plots(
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    radius_km: float = Query(default=20, gt=0, le=200),
    west: float | None = Query(default=None, ge=-180, le=180),
    south: float | None = Query(default=None, ge=-90, le=90),
    east: float | None = Query(default=None, ge=-180, le=180),
    north: float | None = Query(default=None, ge=-90, le=90),
    season: str | None = None,
    crop: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    if (lat is None) != (lng is None):
        raise AppError("INVALID_SEARCH", "lat and lng must be supplied together", 422)
    if lat is None and west is None:
        raise AppError(
            "INVALID_SEARCH", "Provide a map bounding box or a lat/lng search point", 422
        )
    if any(v is not None for v in (west, south, east, north)) and not all(
        v is not None for v in (west, south, east, north)
    ):
        raise AppError("INVALID_BBOX", "west, south, east and north are all required", 422)
    if west is not None and east is not None and west >= east:
        raise AppError("INVALID_BBOX", "west must be less than east", 422)
    if south is not None and north is not None and south >= north:
        raise AppError("INVALID_BBOX", "south must be less than north", 422)
    query = (
        select(Plot, PlotSeason)
        .join(PlotSeason, PlotSeason.plot_id == Plot.id)
        .where(Plot.status == "ACTIVE")
    )
    query = query.where(
        ~select(Reservation.id)
        .where(Reservation.plot_season_id == PlotSeason.id, Reservation.status.in_(LIVE))
        .exists()
    )
    if season:
        query = query.where(PlotSeason.season == season)
    if crop:
        query = query.where(PlotSeason.allowed_crops.any(crop))  # type: ignore[arg-type]
    if west is not None:
        envelope = func.ST_MakeEnvelope(west, south, east, north, 4326)
        query = query.where(Plot.boundary.op("&&")(envelope))
    if lat is not None:
        point = func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326)
        query = query.where(
            func.ST_DWithin(
                cast(Plot.centroid, Geography(srid=4326)),
                cast(point, Geography(srid=4326)),
                radius_km * 1000,
            )
        )
        query = query.order_by(Plot.centroid.op("<->")(point))
    else:
        query = query.order_by(Plot.created_at.desc())
    rows = (await session.execute(query.offset(offset).limit(limit))).all()
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": str(p.id),
                "geometry": geometry_json(p.boundary),
                "properties": {
                    "title": p.title,
                    "areaAcres": float(p.area_acres),
                    "farmerId": str(p.farmer_id),
                    "plotSeasonId": str(s.id),
                    "season": s.season,
                    "startDate": s.start_date.isoformat(),
                    "endDate": s.end_date.isoformat(),
                    "price": float(s.price),
                    "allowedCrops": s.allowed_crops,
                },
            }
            for p, s in rows
        ],
    }


@router.get("/plots/{plot_id}", response_model=PlotOut, tags=["plots"])
async def get_plot(plot_id: UUID, session: AsyncSession = Depends(get_session)):
    return await load_plot(session, plot_id)


@router.patch("/plots/{plot_id}", response_model=PlotOut, tags=["plots"])
async def update_plot(
    plot_id: UUID,
    body: PlotUpdate,
    user: CurrentUser = Depends(farmer_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(Plot, plot_id)
    if row is None or row.status == "DELETED":
        raise AppError("PLOT_NOT_FOUND", "Plot not found", 404)
    if str(row.farmer_id) != user.id and not user.has_any(Role.ADMIN):
        raise AppError("NOT_PLOT_OWNER", "This plot belongs to another farmer", 403)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise AppError("EMPTY_UPDATE", "Provide at least one field to update", 422)
    for field in ("title", "description", "soil_type", "water_source"):
        if field in changes:
            if field == "title" and changes[field] is None:
                raise AppError("VALIDATION_ERROR", "title cannot be null", 422)
            setattr(row, field, changes[field])
    if "boundary" in changes and changes["boundary"] is None:
        raise AppError("VALIDATION_ERROR", "boundary cannot be null", 422)
    if body.boundary is not None:
        boundary = polygon_to_db(body.boundary)
        typed_boundary = cast(boundary, Geometry(geometry_type="POLYGON", srid=4326))
        valid = await session.scalar(select(func.ST_IsValid(typed_boundary)))
        if not valid:
            raise AppError("INVALID_PLOT_BOUNDARY", "PostGIS rejected the plot polygon", 422)
        overlap = await session.scalar(
            select(Plot.id)
            .where(
                Plot.id != plot_id,
                Plot.status == "ACTIVE",
                func.ST_Intersects(Plot.boundary, typed_boundary),
            )
            .limit(1)
        )
        if overlap:
            raise AppError(
                "PLOT_OVERLAP",
                "Plot boundary intersects an existing active plot",
                409,
                details={"plotId": str(overlap)},
            )
        row.boundary = boundary
        row.area_acres = await session.scalar(  # type: ignore[assignment]
            select(func.ST_Area(cast(typed_boundary, Geography(srid=4326))) / 4046.8564224)
        )
        row.centroid = await session.scalar(  # type: ignore[assignment]
            select(
                func.ST_Centroid(typed_boundary, type_=Geometry(geometry_type="POINT", srid=4326))
            )
        )
    row.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    return plot_out(row, row.boundary, row.centroid)


@router.delete("/plots/{plot_id}", status_code=204, tags=["plots"])
async def delete_plot(
    plot_id: UUID,
    user: CurrentUser = Depends(farmer_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(Plot, plot_id)
    if row is None or row.status == "DELETED":
        raise AppError("PLOT_NOT_FOUND", "Plot not found", 404)
    if str(row.farmer_id) != user.id and not user.has_any(Role.ADMIN):
        raise AppError("NOT_PLOT_OWNER", "This plot belongs to another farmer", 403)
    if await session.scalar(
        select(Reservation.id)
        .where(
            Reservation.plot_season_id.in_(
                select(PlotSeason.id).where(PlotSeason.plot_id == plot_id)
            ),
            Reservation.status.in_(LIVE),
        )
        .limit(1)
    ):
        raise AppError(
            "PLOT_HAS_LIVE_RESERVATION", "A plot with a live reservation cannot be deleted", 409
        )
    row.status = "DELETED"
    await session.commit()
    return Response(status_code=204)


@router.post(
    "/plots/{plot_id}/seasons", status_code=201, response_model=SeasonOut, tags=["seasons"]
)
async def create_season(
    plot_id: UUID,
    body: SeasonCreate,
    user: CurrentUser = Depends(farmer),
    session: AsyncSession = Depends(get_session),
):
    plot = await session.get(Plot, plot_id)
    if plot is None or plot.status != "ACTIVE":
        raise AppError("PLOT_NOT_FOUND", "Plot not found", 404)
    if str(plot.farmer_id) != user.id:
        raise AppError("NOT_PLOT_OWNER", "This plot belongs to another farmer", 403)
    row = PlotSeason(plot_id=plot_id, **body.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return season_out(row)


@router.get("/plots/{plot_id}/seasons", response_model=list[SeasonOut], tags=["seasons"])
async def list_seasons(plot_id: UUID, session: AsyncSession = Depends(get_session)):
    if await session.get(Plot, plot_id) is None:
        raise AppError("PLOT_NOT_FOUND", "Plot not found", 404)
    rows = (
        (
            await session.execute(
                select(PlotSeason)
                .where(PlotSeason.plot_id == plot_id)
                .order_by(PlotSeason.start_date)
            )
        )
        .scalars()
        .all()
    )
    return [season_out(r) for r in rows]


@router.patch("/plots/{plot_id}/seasons/{season_id}", response_model=SeasonOut, tags=["seasons"])
async def update_season(
    plot_id: UUID,
    season_id: UUID,
    body: SeasonCreate,
    user: CurrentUser = Depends(farmer_admin),
    session: AsyncSession = Depends(get_session),
):
    plot = await session.get(Plot, plot_id)
    season = await session.get(PlotSeason, season_id)
    if plot is None or plot.status != "ACTIVE" or season is None or season.plot_id != plot_id:
        raise AppError("PLOT_SEASON_NOT_FOUND", "Plot season not found", 404)
    if str(plot.farmer_id) != user.id and not user.has_any(Role.ADMIN):
        raise AppError("NOT_PLOT_OWNER", "This plot belongs to another farmer", 403)
    live = await session.scalar(
        select(Reservation.id)
        .where(Reservation.plot_season_id == season_id, Reservation.status.in_(LIVE))
        .limit(1)
    )
    if live:
        raise AppError(
            "SEASON_HAS_LIVE_RESERVATION", "A season with a live reservation cannot be changed", 409
        )
    for key, value in body.model_dump().items():
        setattr(season, key, value)
    await session.commit()
    await session.refresh(season)
    return season_out(season)


@router.delete("/plots/{plot_id}/seasons/{season_id}", status_code=204, tags=["seasons"])
async def delete_season(
    plot_id: UUID,
    season_id: UUID,
    user: CurrentUser = Depends(farmer_admin),
    session: AsyncSession = Depends(get_session),
):
    plot = await session.get(Plot, plot_id)
    season = await session.get(PlotSeason, season_id)
    if plot is None or season is None or season.plot_id != plot_id:
        raise AppError("PLOT_SEASON_NOT_FOUND", "Plot season not found", 404)
    if str(plot.farmer_id) != user.id and not user.has_any(Role.ADMIN):
        raise AppError("NOT_PLOT_OWNER", "This plot belongs to another farmer", 403)
    history = await session.scalar(
        select(Reservation.id).where(Reservation.plot_season_id == season_id).limit(1)
    )
    if history:
        raise AppError(
            "SEASON_HAS_RESERVATION_HISTORY",
            "A season with reservation history cannot be deleted",
            409,
        )
    await session.delete(season)
    await session.commit()
    return Response(status_code=204)


@router.post("/reservations", status_code=201, response_model=ReservationOut, tags=["reservations"])
async def reserve_plot(
    body: ReservationCreate,
    user: CurrentUser = Depends(customer),
    session: AsyncSession = Depends(get_session),
):
    season = await session.get(PlotSeason, body.plot_season_id)
    if season is None:
        raise AppError("PLOT_SEASON_NOT_FOUND", "Plot season not found", 404)
    plot = await session.get(Plot, season.plot_id)
    if plot is None or plot.status != "ACTIVE":
        raise AppError("PLOT_NOT_FOUND", "Plot not found", 404)
    if season.allowed_crops and body.crop_id not in season.allowed_crops:
        raise AppError("CROP_NOT_ALLOWED", "Crop is not allowed in this season", 422)
    row = Reservation(
        plot_season_id=season.id,
        customer_id=UUID(user.id),
        farmer_id=plot.farmer_id,
        crop_id=body.crop_id,
        expected_qty_kg=body.expected_qty_kg,
        total=float(season.price) * body.expected_qty_kg,
        status="PENDING",
        expires_at=datetime.now(UTC) + timedelta(hours=48),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AppError(
            "PLOT_SEASON_ALREADY_RESERVED", "This plot-season already has a live reservation", 409
        ) from exc
    await session.refresh(row)
    return reservation_out(row)


@router.get("/reservations/mine", response_model=list[ReservationOut], tags=["reservations"])
async def my_reservations(
    user: CurrentUser = Depends(require_roles(Role.CUSTOMER, Role.FARMER)),
    session: AsyncSession = Depends(get_session),
):
    q = select(Reservation)
    if user.has_any(Role.FARMER):
        q = q.where(Reservation.farmer_id == UUID(user.id))
    else:
        q = q.where(Reservation.customer_id == UUID(user.id))
    rows = (await session.execute(q.order_by(Reservation.created_at.desc()))).scalars().all()
    return [reservation_out(r) for r in rows]


async def owned_reservation(session, reservation_id, user, roles):
    row = await session.get(Reservation, reservation_id)
    if row is None:
        raise AppError("RESERVATION_NOT_FOUND", "Reservation not found", 404)
    if (
        (Role.FARMER in roles and str(row.farmer_id) == user.id)
        or (Role.CUSTOMER in roles and str(row.customer_id) == user.id)
        or user.has_any(Role.ADMIN)
    ):
        return row
    raise AppError("NOT_RESERVATION_PARTY", "You cannot modify this reservation", 403)


@router.post(
    "/reservations/{reservation_id}/accept", response_model=ReservationOut, tags=["reservations"]
)
async def accept_reservation(
    reservation_id: UUID,
    user: CurrentUser = Depends(farmer_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await owned_reservation(session, reservation_id, user, [Role.FARMER])
    if row.status != "PENDING" or row.expires_at <= datetime.now(UTC):
        raise AppError(
            "RESERVATION_NOT_PENDING", "Only an unexpired pending reservation can be accepted", 409
        )
    season = await session.get(PlotSeason, row.plot_season_id)
    plot = await session.get(Plot, season.plot_id) if season else None
    if season is None or plot is None:
        raise AppError("PLOT_SEASON_NOT_FOUND", "The reserved plot-season no longer exists", 409)
    rate = get_land_settings().deposit_rate
    agreed = build_terms(
        plot_title=plot.title,
        area_acres=float(plot.area_acres),
        season=season.season,
        start_date=season.start_date,
        end_date=season.end_date,
        crop_id=row.crop_id,
        expected_qty_kg=float(row.expected_qty_kg),
        price_per_kg=float(season.price),
        deposit_rate=rate,
    )
    row.status = "ACCEPTED"
    row.total = agreed.total
    row.deposit = agreed.deposit
    agreement = Agreement(
        id=uuid4(),
        reservation_id=row.id,
        farmer_id=row.farmer_id,
        customer_id=row.customer_id,
        plot_id=plot.id,
        plot_title=plot.title,
        season=season.season,
        start_date=season.start_date,
        end_date=season.end_date,
        crop_id=row.crop_id,
        expected_qty_kg=float(row.expected_qty_kg),
        price_per_kg=float(season.price),
        total=agreed.total,
        deposit=agreed.deposit,
        balance=agreed.balance,
        terms=agreed.terms,
    )
    session.add(agreement)
    add_event(
        session,
        OutboxEvent,
        Event(
            type="reservation.accepted",
            producer="land",
            payload={
                "reservationId": str(row.id),
                "customerId": str(row.customer_id),
                "farmerId": str(row.farmer_id),
                "deposit": float(row.deposit),
                "agreementId": str(agreement.id),
            },
        ),
    )
    await session.commit()
    await session.refresh(row)
    return reservation_out(row)


@router.get(
    "/reservations/{reservation_id}/agreement", response_model=AgreementOut, tags=["reservations"]
)
async def get_agreement(
    reservation_id: UUID,
    user: CurrentUser = Depends(require_roles(Role.FARMER, Role.CUSTOMER, Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    """The agreement made when the farmer accepted: price, deposit, balance and terms."""
    await owned_reservation(session, reservation_id, user, [Role.FARMER, Role.CUSTOMER])
    agreement = await session.scalar(
        select(Agreement).where(Agreement.reservation_id == reservation_id)
    )
    if agreement is None:
        raise AppError("AGREEMENT_NOT_FOUND", "There is no agreement until the farmer accepts", 404)
    return AgreementOut.model_validate(agreement, from_attributes=True)


@router.post(
    "/reservations/{reservation_id}/reject", response_model=ReservationOut, tags=["reservations"]
)
async def reject_reservation(
    reservation_id: UUID,
    user: CurrentUser = Depends(farmer_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await owned_reservation(session, reservation_id, user, [Role.FARMER])
    if row.status != "PENDING":
        raise AppError("RESERVATION_NOT_PENDING", "Only pending reservations can be rejected", 409)
    row.status = "REJECTED"
    await session.commit()
    await session.refresh(row)
    return reservation_out(row)


@router.post(
    "/reservations/{reservation_id}/cancel", response_model=ReservationOut, tags=["reservations"]
)
async def cancel_reservation(
    reservation_id: UUID,
    user: CurrentUser = Depends(require_roles(Role.CUSTOMER, Role.FARMER, Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    row = await owned_reservation(session, reservation_id, user, [Role.CUSTOMER, Role.FARMER])
    if row.status not in LIVE:
        raise AppError("RESERVATION_NOT_CANCELLABLE", "Reservation is no longer active", 409)
    row.status = "CANCELLED"
    add_event(
        session,
        OutboxEvent,
        Event(
            type="reservation.cancelled",
            producer="land",
            payload={"reservationId": str(row.id)},
        ),
    )
    await session.commit()
    await session.refresh(row)
    return reservation_out(row)


@router.post("/reservations/{reservation_id}/updates", status_code=201, tags=["crop updates"])
async def add_crop_update(
    reservation_id: UUID,
    body: CropUpdateCreate,
    user: CurrentUser = Depends(farmer),
    session: AsyncSession = Depends(get_session),
):
    row = await owned_reservation(session, reservation_id, user, [Role.FARMER])
    if row.status not in ("ACTIVE", "HARVEST_READY"):
        raise AppError("RESERVATION_NOT_ACTIVE", "Crop updates require an active reservation", 409)
    update = CropUpdate(
        reservation_id=row.id,
        stage=body.stage,
        note=body.note,
        media_ids=body.media_ids,
        location=point_to_db(body.location) if body.location else None,
    )
    session.add(update)
    await session.commit()
    await session.refresh(update)
    return {
        "id": str(update.id),
        "reservationId": str(row.id),
        "stage": update.stage,
        "note": update.note,
        "mediaIds": [str(x) for x in update.media_ids],
        "createdAt": update.created_at,
    }


@router.get("/reservations/{reservation_id}/updates", tags=["crop updates"])
async def list_crop_updates(
    reservation_id: UUID,
    user: CurrentUser = Depends(require_roles(Role.CUSTOMER, Role.FARMER, Role.ADMIN)),
    session: AsyncSession = Depends(get_session),
):
    await owned_reservation(session, reservation_id, user, [Role.CUSTOMER, Role.FARMER])
    rows = (
        (
            await session.execute(
                select(CropUpdate)
                .where(CropUpdate.reservation_id == reservation_id)
                .order_by(CropUpdate.created_at, CropUpdate.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(item.id),
            "reservationId": str(item.reservation_id),
            "stage": item.stage,
            "note": item.note,
            "mediaIds": [str(media_id) for media_id in (item.media_ids or [])],
            "location": geometry_json(item.location) if item.location is not None else None,
            "createdAt": item.created_at,
        }
        for item in rows
    ]


@router.post(
    "/reservations/{reservation_id}/harvest-ready", response_model=ReservationOut, tags=["harvest"]
)
async def harvest_ready(
    reservation_id: UUID,
    user: CurrentUser = Depends(farmer),
    session: AsyncSession = Depends(get_session),
):
    row = await owned_reservation(session, reservation_id, user, [Role.FARMER])
    if row.status != "ACTIVE":
        raise AppError(
            "RESERVATION_NOT_ACTIVE", "Only an active reservation can be marked harvest-ready", 409
        )
    season = await session.get(PlotSeason, row.plot_season_id)
    plot = await session.get(Plot, season.plot_id) if season else None
    row.harvest_attempt += 1
    row.status = "HARVEST_READY"
    add_event(
        session,
        OutboxEvent,
        Event(
            type="harvest.ready",
            producer="land",
            payload={
                "reservationId": str(row.id),
                "farmerId": str(row.farmer_id),
                "customerId": str(row.customer_id),
                "cropId": row.crop_id,
                "expectedQtyKg": float(row.expected_qty_kg),
                "attempt": row.harvest_attempt,
                "plotCentroid": geometry_json(plot.centroid) if plot else None,
            },
        ),
    )
    await session.commit()
    await session.refresh(row)
    return reservation_out(row)
