"""media: photos with EXIF/GPS verification, outbox

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "media"
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "media",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_type", sa.String(20), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_type", sa.String(40), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(200), nullable=True),
        sa.Column("original_key", sa.String(200), nullable=False),
        sa.Column("public_key", sa.String(200), nullable=True),
        sa.Column("thumb_key", sa.String(200), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("taken_at", TS, nullable=True),
        sa.Column("gps_lat", sa.Float(), nullable=True),
        sa.Column("gps_lng", sa.Float(), nullable=True),
        sa.Column("geo_status", sa.String(16), nullable=False),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("reject_reason", sa.String(40), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("processed_at", TS, nullable=True),
        schema=SCHEMA,
    )
    op.create_index("media_owner_ix", "media", ["owner_type", "owner_id"], schema=SCHEMA)
    op.create_index("media_status_created_ix", "media", ["status", "created_at"], schema=SCHEMA)
    op.create_index("ix_media_media_uploaded_by", "media", ["uploaded_by"], schema=SCHEMA)
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("producer", sa.String(50), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("published_at", TS, nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_media_outbox_type", "outbox", ["type"], schema=SCHEMA)
    op.create_index("ix_media_outbox_occurred_at", "outbox", ["occurred_at"], schema=SCHEMA)
    op.create_index("ix_media_outbox_published_at", "outbox", ["published_at"], schema=SCHEMA)
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("processed_at", TS, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )


def downgrade() -> None:
    for table in ("processed_events", "outbox", "media"):
        op.drop_table(table, schema=SCHEMA)
