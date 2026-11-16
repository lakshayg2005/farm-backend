"""Database models for the order service live in the `order` schema only."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from farm_common.outbox import OutboxMixin, ProcessedEventMixin

SCHEMA = "order"

_JSON = JSON().with_variant(JSONB(), "postgresql")


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class PaymentRow(Base):
    """A payment (HLD payments table). The mock intent is created by POST /orders/payments/intent
    and settled by checkout; a REFUND row is written when an order is cancelled."""

    __tablename__ = "payments"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    kind: Mapped[str] = mapped_column(String(20))  # FULL, DEPOSIT, BALANCE or REFUND
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    # reservation id for DEPOSIT / BALANCE (V2); the checkout id once a FULL payment is settled
    reference_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    order_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CheckoutRow(Base):
    __tablename__ = "checkouts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    payment_intent_id: Mapped[UUID] = mapped_column(Uuid)
    payment_status: Mapped[str] = mapped_column(String(20))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    orders: Mapped[list["OrderRow"]] = relationship(
        back_populates="checkout", order_by="OrderRow.created_at, OrderRow.id"
    )


class OrderRow(Base):
    """One order per farm inside a checkout."""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_customer_created", "customer_id", "created_at"),
        Index("ix_orders_farm_owner_created", "farm_owner_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    checkout_id: Mapped[UUID] = mapped_column(ForeignKey(f"{SCHEMA}.checkouts.id"), index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid)
    farm_id: Mapped[UUID] = mapped_column(Uuid)
    # Owner (user id) of the farm, copied from the catalog at reserve time so a farmer can be
    # matched to their orders without calling catalog.
    farm_owner_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    order_type: Mapped[str] = mapped_column(String(20), default="PRODUCE")
    status: Mapped[str] = mapped_column(String(30), index=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    delivery_address: Mapped[dict[str, Any]] = mapped_column(_JSON)
    delivery_date: Mapped[date] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    checkout: Mapped[CheckoutRow] = relationship(back_populates="orders")
    items: Mapped[list["OrderItemRow"]] = relationship(
        back_populates="order", order_by="OrderItemRow.id"
    )
    events: Mapped[list["OrderEventRow"]] = relationship(
        back_populates="order", order_by="OrderEventRow.id"
    )
    rating: Mapped["RatingRow | None"] = relationship(back_populates="order")


class OrderItemRow(Base):
    """Name, unit and price are copied from the listing when stock is reserved."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[UUID] = mapped_column(ForeignKey(f"{SCHEMA}.orders.id"), index=True)
    listing_id: Mapped[UUID] = mapped_column(Uuid)
    farm_id: Mapped[UUID] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # GeoJSON point from catalog. Kept as JSON: this service does no spatial queries; logistics
    # clusters the points it receives in order.ready_for_dispatch.
    pickup_location: Mapped[dict[str, Any]] = mapped_column(_JSON)

    order: Mapped[OrderRow] = relationship(back_populates="items")


class OrderEventRow(Base):
    """Status timeline of an order (order_events). Append only."""

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[UUID] = mapped_column(ForeignKey(f"{SCHEMA}.orders.id"), index=True)
    status: Mapped[str] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    order: Mapped[OrderRow] = relationship(back_populates="events")


class RatingRow(Base):
    __tablename__ = "order_ratings"

    order_id: Mapped[UUID] = mapped_column(ForeignKey(f"{SCHEMA}.orders.id"), primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid)
    score: Mapped[int] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    order: Mapped[OrderRow] = relationship(back_populates="rating")


class IdempotencyKeyRow(Base):
    """Remembers the result of POST /orders per (customer, Idempotency-Key)."""

    __tablename__ = "idempotency_keys"

    customer_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20))  # IN_PROGRESS or DONE
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedEventMixin, Base):
    __tablename__ = "processed_events"
