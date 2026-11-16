"""Database models for the media service live in the `media` schema only."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import JSON, DateTime, Float, Index, Integer, MetaData, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from farm_common.outbox import OutboxMixin, ProcessedEventMixin

SCHEMA = "media"


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Media(Base):
    """One uploaded photo (HLD section 10: id, owner_service, owner_id, url, thumb_url, exif_time,
    is_geo_verified, taken_at POINT).

    The original keeps its EXIF/GPS and stays private; `url` and `thumb_url` point at re-encoded
    copies without EXIF that anyone can read.
    """

    __tablename__ = "media"
    __table_args__ = (
        Index("media_owner_ix", "owner_service", "owner_type", "owner_id"),
        Index("media_status_created_ix", "status", "created_at"),
        Index("media_taken_at_gix", "taken_at", postgresql_using="gist"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_service: Mapped[str] = mapped_column(String(20))  # catalog, land, quality, ...
    owner_type: Mapped[str] = mapped_column(String(20))  # FARM, LISTING, PLOT, ...
    owner_id: Mapped[UUID] = mapped_column(Uuid)
    uploaded_by: Mapped[UUID] = mapped_column(Uuid, index=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING_UPLOAD")
    content_type: Mapped[str] = mapped_column(String(40))
    size_bytes: Mapped[int] = mapped_column(Integer)
    file_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    original_key: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thumb_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exif_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Where the photo was taken (EXIF GPS). Kept server-side only, never on public routes.
    taken_at: Mapped[WKBElement | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=True
    )
    is_geo_verified: Mapped[bool] = mapped_column(default=False)
    geo_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyKey(Base):
    """First response to a write sent with an Idempotency-Key, replayed on retries (HLD 15)."""

    __tablename__ = "idempotency_keys"

    user_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int]
    response: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedEventMixin, Base):
    __tablename__ = "processed_events"
