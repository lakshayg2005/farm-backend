"""delivery partner home point (for nearest-partner assignment)

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "logistics"


def upgrade() -> None:
    op.add_column(
        "delivery_partners", sa.Column("home_lat", sa.Float(), nullable=True), schema=SCHEMA
    )
    op.add_column(
        "delivery_partners", sa.Column("home_lng", sa.Float(), nullable=True), schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_column("delivery_partners", "home_lng", schema=SCHEMA)
    op.drop_column("delivery_partners", "home_lat", schema=SCHEMA)
