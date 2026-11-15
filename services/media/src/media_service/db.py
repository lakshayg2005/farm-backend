"""Database models for the media service live in the `media` schema only."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, Index, Integer, MetaData, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from farm_common.outbox import OutboxMixin, ProcessedEventMixin

SCHEMA = "media"


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Media(Base):
    """One uploaded photo. The original (with GPS) stays private; `public_key` and `thumb_key`
    are re-encoded copies without EXIF that anyone can read."""

    __tablename__ = "media"
    __table_args__ = (
        Index("media_owner_ix", "owner_type", "owner_id"),
        Index("media_status_created_ix", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_type: Mapped[str] = mapped_column(String(20))
    owner_id: Mapped[UUID] = mapped_column(Uuid)
    uploaded_by: Mapped[UUID] = mapped_column(Uuid, index=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING_UPLOAD")
    content_type: Mapped[str] = mapped_column(String(40))
    size_bytes: Mapped[int] = mapped_column(Integer)
    file_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    original_key: Mapped[str] = mapped_column(String(200))
    public_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    thumb_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # From EXIF. Media runs no spatial queries (the 2 km check is done per photo in Python),
    # so plain coordinates are enough here; they are never exposed on public routes.
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    geo_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedEventMixin, Base):
    __tablename__ = "processed_events"
