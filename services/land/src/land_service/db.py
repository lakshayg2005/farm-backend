"""Database models for plots, seasons, reservations and crop updates."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from farm_common.outbox import OutboxMixin, ProcessedEventMixin

SCHEMA = "land"


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Plot(Base):
    __tablename__ = "plots"
    __table_args__ = (
        CheckConstraint("area_acres > 0", name="plot_area_positive"),
        Index("plots_boundary_gix", "boundary", postgresql_using="gist"),
        Index("plots_centroid_gix", "centroid", postgresql_using="gist"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    farmer_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    area_acres: Mapped[float] = mapped_column(Numeric(12, 4))
    soil_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    water_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    boundary: Mapped[WKBElement] = mapped_column(
        Geometry("POLYGON", srid=4326, spatial_index=False)
    )
    centroid: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326, spatial_index=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class PlotSeason(Base):
    __tablename__ = "plot_seasons"
    __table_args__ = (
        CheckConstraint("end_date > start_date", name="season_dates_ordered"),
        CheckConstraint("price > 0", name="season_price_positive"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    plot_id: Mapped[UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.plots.id", ondelete="CASCADE"), index=True
    )
    season: Mapped[str] = mapped_column(String(40))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    price: Mapped[float] = mapped_column(Numeric(12, 2))
    allowed_crops: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("expected_qty_kg > 0", name="reservation_qty_positive"),
        Index(
            "one_live_reservation_per_plot_season",
            "plot_season_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING','ACCEPTED','ACTIVE','HARVEST_READY')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    plot_season_id: Mapped[UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.plot_seasons.id"), index=True
    )
    customer_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    farmer_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    crop_id: Mapped[str] = mapped_column(String(40))
    expected_qty_kg: Mapped[float] = mapped_column(Numeric(12, 3))
    deposit: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    total: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    harvest_attempt: Mapped[int] = mapped_column(default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CropUpdate(Base):
    __tablename__ = "crop_updates"
    __table_args__ = (Index("crop_updates_location_gix", "location", postgresql_using="gist"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.reservations.id"), index=True
    )
    stage: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), default=list)
    location: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Agreement(Base):
    """What farmer and customer agreed when the reservation was accepted (price, deposit, terms)."""

    __tablename__ = "agreements"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.reservations.id", ondelete="CASCADE"), unique=True
    )
    farmer_id: Mapped[UUID] = mapped_column(Uuid)
    customer_id: Mapped[UUID] = mapped_column(Uuid)
    plot_id: Mapped[UUID] = mapped_column(Uuid)
    plot_title: Mapped[str] = mapped_column(String(120))
    season: Mapped[str] = mapped_column(String(40))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    crop_id: Mapped[str] = mapped_column(String(40))
    expected_qty_kg: Mapped[float] = mapped_column(Numeric(12, 3))
    price_per_kg: Mapped[float] = mapped_column(Numeric(12, 2))
    total: Mapped[float] = mapped_column(Numeric(12, 2))
    deposit: Mapped[float] = mapped_column(Numeric(12, 2))
    balance: Mapped[float] = mapped_column(Numeric(12, 2))
    terms: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedEventMixin, Base):
    __tablename__ = "processed_events"
