"""Database models for the logistics service live in the `logistics` schema only.

HLD section 10 tables:
  delivery_partners  – who delivers things
  delivery_pools     – one pool per DBSCAN cluster per date (route + stats)
  pool_stops         – ordered waypoints inside a pool (PICKUP or DROP)
  location_pings     – partner GPS stream while on a route
  outbox / processed_events – transactional outbox pattern (farm_common)
"""

from datetime import UTC, datetime
from datetime import date as _date
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from farm_common.outbox import OutboxMixin, ProcessedEventMixin

SCHEMA = "logistics"


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


# ---------------------------------------------------------------------------
# Domain enums (mirrored as Python StrEnum; stored as VARCHAR in Postgres)
# ---------------------------------------------------------------------------


class PoolStatus(StrEnum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class StopType(StrEnum):
    PICKUP = "PICKUP"
    DROP = "DROP"


class StopStatus(StrEnum):
    PENDING = "PENDING"
    ARRIVED = "ARRIVED"
    DONE = "DONE"
    SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DeliveryPartner(Base):
    """A delivery agent who can be assigned to pools.

    HLD section 16 seeds 30 partners; this table is managed by the identity
    service and replicated here for read convenience.
    """

    __tablename__ = "delivery_partners"
    __table_args__ = (Index("dp_online_ix", "is_online"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    vehicle_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    capacity_kg: Mapped[float] = mapped_column(Float, default=50.0)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    # home point from identity, used to prefer the partner nearest a pool's first pickup
    home_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DeliveryPool(Base):
    """One DBSCAN-generated delivery pool for a given date.

    geom        – the driving route as a LINESTRING (from OSRM)
    total_km    – route length in kilometres
    km_saved    – vs. each order having its own trip (sum of individual
                  hub→drop distances minus the pool route distance)
    """

    __tablename__ = "delivery_pools"
    __table_args__ = (
        Index("pool_date_ix", "pool_date"),
        Index("pool_partner_date_ix", "partner_id", "pool_date"),
        Index("pool_geom_gix", "geom", postgresql_using="gist"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    pool_date: Mapped[_date] = mapped_column(Date, index=True)
    partner_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default=PoolStatus.PENDING)
    # PostGIS LINESTRING for the driving route
    geom: Mapped[WKBElement | None] = mapped_column(
        Geometry("LINESTRING", srid=4326, spatial_index=False), nullable=True
    )
    total_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    km_saved: Mapped[float | None] = mapped_column(Float, nullable=True)
    # OSRM raw response (duration etc.) stored for debugging
    osrm_meta: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PoolStop(Base):
    """One waypoint inside a pool – either pickup from hub or drop to customer.

    seq     – 1-based order along the route (0 = hub origin)
    geom    – POINT of the stop location
    eta     – estimated arrival (UTC)
    done_at – when the partner tapped "Done"
    """

    __tablename__ = "pool_stops"
    __table_args__ = (
        Index("stop_pool_seq_ix", "pool_id", "seq"),
        Index("stop_order_ix", "order_id"),
        Index("stop_geom_gix", "geom", postgresql_using="gist"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    pool_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    order_id: Mapped[UUID] = mapped_column(Uuid)
    stop_type: Mapped[str] = mapped_column(String(8))  # PICKUP | DROP
    seq: Mapped[int] = mapped_column(SmallInteger)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    geom: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=True
    )
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default=StopStatus.PENDING)
    # Proof of delivery (photo media_id + 4-digit OTP)
    photo_media_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    otp: Mapped[str | None] = mapped_column(String(6), nullable=True)
    otp_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(300), nullable=True)


class LocationPing(Base):
    """GPS ping from a delivery partner while on a route.

    HLD section 15: POST /logistics/location, stored and forwarded for live
    tracking (GET /logistics/orders/{id}/track).
    """

    __tablename__ = "location_pings"
    __table_args__ = (
        Index("ping_partner_recorded_ix", "partner_id", "recorded_at"),
        Index("ping_pool_recorded_ix", "pool_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partner_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    pool_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ---------------------------------------------------------------------------
# Outbox / processed-events (transactional outbox pattern, farm_common)
# ---------------------------------------------------------------------------


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedEventMixin, Base):
    __tablename__ = "processed_events"
