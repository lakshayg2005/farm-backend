"""Reservation agreements: price, deposit and terms, written when a farmer accepts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_agreements"
down_revision = "0002_harvest_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agreements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "reservation_id",
            sa.Uuid(),
            sa.ForeignKey("land.reservations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("farmer_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("plot_id", sa.Uuid(), nullable=False),
        sa.Column("plot_title", sa.String(120), nullable=False),
        sa.Column("season", sa.String(40), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("crop_id", sa.String(40), nullable=False),
        sa.Column("expected_qty_kg", sa.Numeric(12, 3), nullable=False),
        sa.Column("price_per_kg", sa.Numeric(12, 2), nullable=False),
        sa.Column("total", sa.Numeric(12, 2), nullable=False),
        sa.Column("deposit", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance", sa.Numeric(12, 2), nullable=False),
        sa.Column("terms", JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="land",
    )


def downgrade() -> None:
    op.drop_table("agreements", schema="land")
