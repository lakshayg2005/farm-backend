"""Database models for the catalog service live in the `catalog` schema only."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
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
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from farm_common.outbox import OutboxMixin, ProcessedEventMixin

SCHEMA = "catalog"
QTY = Numeric(12, 3)
MONEY = Numeric(10, 2)


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Crop(Base):
    __tablename__ = "crops"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(20))
    default_unit: Mapped[str] = mapped_column(String(10))


class Farm(Base):
    __tablename__ = "farms"
    __table_args__ = (Index("farms_location_gix", "location", postgresql_using="gist"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    farmer_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    name: Mapped[str] = mapped_column(String(120))
    village: Mapped[str | None] = mapped_column(String(80), nullable=True)
    district: Mapped[str] = mapped_column(String(80))
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    location: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326, spatial_index=False))
    boundary: Mapped[WKBElement | None] = mapped_column(
        Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Listing(Base):
    __tablename__ = "produce_listings"
    __table_args__ = (
        # The database itself refuses to oversell, whatever the code does.
        CheckConstraint("qty_available >= 0", name="qty_available_non_negative"),
        CheckConstraint("price_per_unit > 0", name="price_positive"),
        Index("listings_status_crop_ix", "status", "crop_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    farm_id: Mapped[UUID] = mapped_column(ForeignKey(Farm.id), index=True)
    # Copied from the farm so ownership checks need no join.
    farmer_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    crop_id: Mapped[str] = mapped_column(ForeignKey(Crop.id))
    variety: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    qty_available: Mapped[Decimal] = mapped_column(QTY)
    unit: Mapped[str] = mapped_column(String(10))
    price_per_unit: Mapped[Decimal] = mapped_column(MONEY)
    harvest_date: Mapped[date] = mapped_column(Date)
    grade: Mapped[str | None] = mapped_column(String(1), nullable=True)
    is_organic: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE")
    pickup_location: Mapped[WKBElement] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False)
    )
    media_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    farm: Mapped[Farm] = relationship(lazy="joined", innerjoin=True)
    crop: Mapped[Crop] = relationship(lazy="joined", innerjoin=True)


# Distance search casts to geography, so index that expression: a plain GiST index on the geometry
# column is not used by ST_DWithin(geography, ...).
Index("listings_pickup_gix", func.geography(Listing.pickup_location), postgresql_using="gist")


class StockReservation(Base):
    """One row per order that reserved stock; makes reserve and release idempotent."""

    __tablename__ = "stock_reservations"

    order_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(10))  # RESERVED | RELEASED
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StockMovement(Base):
    """Audit trail of every stock change; the sum of deltas equals qty_available."""

    __tablename__ = "stock_movements"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    listing_id: Mapped[UUID] = mapped_column(ForeignKey(Listing.id), index=True)
    order_id: Mapped[UUID | None] = mapped_column(Uuid, index=True, nullable=True)
    delta: Mapped[Decimal] = mapped_column(QTY)
    reason: Mapped[str] = mapped_column(String(12))  # INITIAL | ADJUSTMENT | RESERVE | RELEASE
    # Price at reservation time, so a repeated reserve call answers with the same price.
    unit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedEventMixin, Base):
    __tablename__ = "processed_events"
