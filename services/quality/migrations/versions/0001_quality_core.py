"""Create quality inspector locations, inspections and event tables."""

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision = "0001_quality_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inspector_locations",
        sa.Column("inspector_id", sa.Uuid(), primary_key=True),
        sa.Column("location", Geometry("POINT", srid=4326), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="quality",
    )
    op.create_index(
        "ix_quality_inspector_locations_available",
        "inspector_locations",
        ["available"],
        schema="quality",
    )
    op.create_index(
        "inspector_location_gix",
        "inspector_locations",
        ["location"],
        schema="quality",
        postgresql_using="gist",
    )
    op.create_table(
        "inspections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("inspector_id", sa.Uuid()),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("grade", sa.String(1)),
        sa.Column("moisture_pct", sa.Float()),
        sa.Column("pest_found", sa.Boolean()),
        sa.Column("weight_kg", sa.Float()),
        sa.Column("result", sa.String(12)),
        sa.Column("resolution", sa.String(24)),
        sa.Column("media_ids", sa.JSON(), nullable=False),
        sa.Column("location", Geometry("POINT", srid=4326)),
        sa.Column("report_note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("subject_type", "subject_id", name="inspection_subject_unique"),
        schema="quality",
    )
    op.create_index(
        "ix_quality_inspections_subject_id", "inspections", ["subject_id"], schema="quality"
    )
    op.create_index(
        "ix_quality_inspections_inspector_id", "inspections", ["inspector_id"], schema="quality"
    )
    op.create_index("ix_quality_inspections_status", "inspections", ["status"], schema="quality")
    op.create_index(
        "inspection_assignee_status_ix", "inspections", ["inspector_id", "status"], schema="quality"
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("producer", sa.String(50), nullable=False),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        schema="quality",
    )
    op.create_index("ix_quality_outbox_type", "outbox", ["type"], schema="quality")
    op.create_index("ix_quality_outbox_occurred_at", "outbox", ["occurred_at"], schema="quality")
    op.create_index("ix_quality_outbox_published_at", "outbox", ["published_at"], schema="quality")
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        schema="quality",
    )


def downgrade() -> None:
    op.drop_table("processed_events", schema="quality")
    op.drop_table("outbox", schema="quality")
    op.drop_table("inspections", schema="quality")
    op.drop_table("inspector_locations", schema="quality")
