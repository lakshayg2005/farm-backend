"""Database models for the identity service live in the `identity` schema only."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, ForeignKey, MetaData, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from farm_common.outbox import OutboxMixin, ProcessedEventMixin

SCHEMA = "identity"


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(16), unique=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    phone_verified: Mapped[bool] = mapped_column(default=False)
    farmer_verified: Mapped[bool] = mapped_column(default=False)
    # Plain coordinates are enough here; services that run geo queries keep their own PostGIS copy.
    home_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    role_rows: Mapped[list["UserRole"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def roles(self) -> list[str]:
        return sorted(r.role for r in self.role_rows)

    def add_role(self, role: str) -> bool:
        if role in self.roles:
            return False
        self.role_rows.append(UserRole(role=role))
        return True

    def set_roles(self, roles: list[str]) -> None:
        wanted = set(roles)
        for row in [r for r in self.role_rows if r.role not in wanted]:
            self.role_rows.remove(row)
        for role in sorted(wanted - set(self.roles)):
            self.role_rows.append(UserRole(role=role))


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[UUID] = mapped_column(ForeignKey(User.id, ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), primary_key=True, index=True)


class RefreshToken(Base):
    """One row per issued refresh token (id = the token's jti), so tokens can be revoked."""

    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey(User.id, ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OtpCode(Base):
    __tablename__ = "otp_codes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    phone: Mapped[str] = mapped_column(String(16), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedEventMixin, Base):
    __tablename__ = "processed_events"
