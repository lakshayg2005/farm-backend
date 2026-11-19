"""Create land plots, seasons, reservations, crop updates and event tables."""

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision = "0001_land_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("farmer_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("area_acres", sa.Numeric(12, 4), nullable=False),
        sa.Column("soil_type", sa.String(60)),
        sa.Column("water_source", sa.String(60)),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("boundary", Geometry("POLYGON", srid=4326), nullable=False),
        sa.Column("centroid", Geometry("POINT", srid=4326), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("area_acres > 0", name="plot_area_positive"),
        schema="land",
    )
    op.create_index("ix_land_plots_farmer_id", "plots", ["farmer_id"], schema="land")
    op.create_index("ix_land_plots_status", "plots", ["status"], schema="land")
    op.create_index(
        "plots_boundary_gix", "plots", ["boundary"], schema="land", postgresql_using="gist"
    )
    op.create_index(
        "plots_centroid_gix", "plots", ["centroid"], schema="land", postgresql_using="gist"
    )

    op.create_table(
        "plot_seasons",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "plot_id", sa.Uuid(), sa.ForeignKey("land.plots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("season", sa.String(40), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("allowed_crops", sa.ARRAY(sa.String(40)), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("end_date > start_date", name="season_dates_ordered"),
        sa.CheckConstraint("price > 0", name="season_price_positive"),
        schema="land",
    )
    op.create_index("ix_land_plot_seasons_plot_id", "plot_seasons", ["plot_id"], schema="land")

    op.create_table(
        "reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "plot_season_id", sa.Uuid(), sa.ForeignKey("land.plot_seasons.id"), nullable=False
        ),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("farmer_id", sa.Uuid(), nullable=False),
        sa.Column("crop_id", sa.String(40), nullable=False),
        sa.Column("expected_qty_kg", sa.Numeric(12, 3), nullable=False),
        sa.Column("deposit", sa.Numeric(12, 2)),
        sa.Column("total", sa.Numeric(12, 2)),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expected_qty_kg > 0", name="reservation_qty_positive"),
        schema="land",
    )
    op.create_index(
        "ix_land_reservations_plot_season_id", "reservations", ["plot_season_id"], schema="land"
    )
    op.create_index(
        "ix_land_reservations_customer_id", "reservations", ["customer_id"], schema="land"
    )
    op.create_index("ix_land_reservations_farmer_id", "reservations", ["farmer_id"], schema="land")
    op.create_index("ix_land_reservations_status", "reservations", ["status"], schema="land")
    op.create_index(
        "one_live_reservation_per_plot_season",
        "reservations",
        ["plot_season_id"],
        unique=True,
        schema="land",
        postgresql_where=sa.text("status IN ('PENDING','ACCEPTED','ACTIVE','HARVEST_READY')"),
    )

    op.create_table(
        "crop_updates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "reservation_id", sa.Uuid(), sa.ForeignKey("land.reservations.id"), nullable=False
        ),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("media_ids", sa.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("location", Geometry("POINT", srid=4326)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="land",
    )
    op.create_index(
        "ix_land_crop_updates_reservation_id", "crop_updates", ["reservation_id"], schema="land"
    )
    op.create_index(
        "crop_updates_location_gix",
        "crop_updates",
        ["location"],
        schema="land",
        postgresql_using="gist",
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
        schema="land",
    )
    op.create_index("ix_land_outbox_type", "outbox", ["type"], schema="land")
    op.create_index("ix_land_outbox_occurred_at", "outbox", ["occurred_at"], schema="land")
    op.create_index("ix_land_outbox_published_at", "outbox", ["published_at"], schema="land")
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        schema="land",
    )


def downgrade() -> None:
    op.drop_table("processed_events", schema="land")
    op.drop_table("outbox", schema="land")
    op.drop_table("crop_updates", schema="land")
    op.drop_table("reservations", schema="land")
    op.drop_table("plot_seasons", schema="land")
    op.drop_table("plots", schema="land")
