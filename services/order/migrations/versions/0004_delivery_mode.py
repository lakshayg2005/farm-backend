"""delivery mode: IMMEDIATE or FLEXIBLE (waits for the daily shared-delivery batch)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # every existing order was routed as soon as it was packed: IMMEDIATE
    op.add_column(
        "orders",
        sa.Column(
            "delivery_mode", sa.String(length=10), nullable=False, server_default="IMMEDIATE"
        ),
        schema="order",
    )


def downgrade() -> None:
    op.drop_column("orders", "delivery_mode", schema="order")
