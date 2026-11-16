"""media: photos with EXIF/GPS verification, idempotency keys, outbox

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "media"
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # PostGIS is normally created once by the database init script; this is a no-op then.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "media",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_service", sa.String(20), nullable=False),
        sa.Column("owner_type", sa.String(20), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_type", sa.String(40), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(200), nullable=True),
        sa.Column("original_key", sa.String(200), nullable=False),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("thumb_url", sa.String(500), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("exif_time", TS, nullable=True),
        sa.Column("taken_at", Geometry("POINT", srid=4326, spatial_index=False), nullable=True),
        sa.Column("is_geo_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("geo_status", sa.String(16), nullable=False),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("reject_reason", sa.String(40), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("processed_at", TS, nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "media_owner_ix", "media", ["owner_service", "owner_type", "owner_id"], schema=SCHEMA
    )
    op.create_index("media_status_created_ix", "media", ["status", "created_at"], schema=SCHEMA)
    op.create_index("ix_media_media_uploaded_by", "media", ["uploaded_by"], schema=SCHEMA)
    op.create_index(
        "media_taken_at_gix", "media", ["taken_at"], schema=SCHEMA, postgresql_using="gist"
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
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
    for table in ("processed_events", "outbox", "idempotency_keys", "media"):
        op.drop_table(table, schema=SCHEMA)
