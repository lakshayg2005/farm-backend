"""catalog: crops, farms, produce listings, stock movements and reservations, outbox

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

SCHEMA = "catalog"
TS = sa.DateTime(timezone=True)
QTY = sa.Numeric(12, 3)
MONEY = sa.Numeric(10, 2)

# Crop ids are stable slugs shared with land and the seed data: never rename one, add new ones
# in a later migration.
CROPS = [
    # vegetables
    ("tomato", "Tomato", "vegetable", "kg"),
    ("onion", "Onion", "vegetable", "kg"),
    ("potato", "Potato", "vegetable", "kg"),
    ("brinjal", "Brinjal", "vegetable", "kg"),
    ("okra", "Okra (Bhindi)", "vegetable", "kg"),
    ("cauliflower", "Cauliflower", "vegetable", "piece"),
    ("cabbage", "Cabbage", "vegetable", "piece"),
    ("carrot", "Carrot", "vegetable", "kg"),
    ("green-chilli", "Green Chilli", "vegetable", "kg"),
    ("bottle-gourd", "Bottle Gourd (Lauki)", "vegetable", "piece"),
    ("bitter-gourd", "Bitter Gourd (Karela)", "vegetable", "kg"),
    ("cucumber", "Cucumber", "vegetable", "kg"),
    ("pumpkin", "Pumpkin", "vegetable", "kg"),
    ("peas", "Green Peas", "vegetable", "kg"),
    ("spinach", "Spinach (Palak)", "leafy", "bunch"),
    ("coriander", "Coriander", "leafy", "bunch"),
    ("methi", "Fenugreek (Methi)", "leafy", "bunch"),
    # fruits
    ("mango", "Mango", "fruit", "kg"),
    ("banana", "Banana", "fruit", "dozen"),
    ("guava", "Guava", "fruit", "kg"),
    ("papaya", "Papaya", "fruit", "kg"),
    ("watermelon", "Watermelon", "fruit", "piece"),
    # grains and pulses
    ("rice", "Rice (Paddy)", "grain", "quintal"),
    ("wheat", "Wheat", "grain", "quintal"),
    ("maize", "Maize", "grain", "quintal"),
    ("millet", "Millet (Bajra)", "grain", "quintal"),
    ("arhar", "Pigeon Pea (Arhar)", "pulse", "quintal"),
    ("chana", "Chickpea (Chana)", "pulse", "quintal"),
    ("moong", "Green Gram (Moong)", "pulse", "quintal"),
    # oilseeds and others
    ("soybean", "Soybean", "oilseed", "quintal"),
    ("groundnut", "Groundnut", "oilseed", "quintal"),
    ("mustard", "Mustard", "oilseed", "quintal"),
    ("sugarcane", "Sugarcane", "cash", "quintal"),
    ("garlic", "Garlic", "spice", "kg"),
    ("ginger", "Ginger", "spice", "kg"),
    ("turmeric", "Turmeric", "spice", "kg"),
]


def upgrade() -> None:
    # PostGIS is normally created once by the database init script; this is a no-op then.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    crops = op.create_table(
        "crops",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("default_unit", sa.String(10), nullable=False),
        schema=SCHEMA,
    )
    op.bulk_insert(
        crops,
        [{"id": i, "name": n, "category": c, "default_unit": u} for i, n, c, u in CROPS],
    )

    op.create_table(
        "farms",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("farmer_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("village", sa.String(80), nullable=True),
        sa.Column("district", sa.String(80), nullable=False),
        sa.Column("state", sa.String(80), nullable=True),
        sa.Column("location", Geometry("POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column(
            "boundary", Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_catalog_farms_farmer_id", "farms", ["farmer_id"], schema=SCHEMA)
    op.create_index(
        "farms_location_gix", "farms", ["location"], schema=SCHEMA, postgresql_using="gist"
    )

    op.create_table(
        "produce_listings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("farm_id", sa.Uuid(), sa.ForeignKey(f"{SCHEMA}.farms.id"), nullable=False),
        sa.Column("farmer_id", sa.Uuid(), nullable=False),
        sa.Column("crop_id", sa.String(40), sa.ForeignKey(f"{SCHEMA}.crops.id"), nullable=False),
        sa.Column("variety", sa.String(80), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("qty_available", QTY, nullable=False),
        sa.Column("unit", sa.String(10), nullable=False),
        sa.Column("price_per_unit", MONEY, nullable=False),
        sa.Column("harvest_date", sa.Date(), nullable=False),
        sa.Column("grade", sa.String(1), nullable=True),
        sa.Column("is_organic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(10), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "pickup_location", Geometry("POINT", srid=4326, spatial_index=False), nullable=False
        ),
        sa.Column(
            "media_ids",
            postgresql.ARRAY(sa.Uuid()),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        # The database itself refuses to oversell, whatever the code does.
        sa.CheckConstraint("qty_available >= 0", name="qty_available_non_negative"),
        sa.CheckConstraint("price_per_unit > 0", name="price_positive"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_catalog_produce_listings_farm_id", "produce_listings", ["farm_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_catalog_produce_listings_farmer_id", "produce_listings", ["farmer_id"], schema=SCHEMA
    )
    op.create_index(
        "listings_status_crop_ix", "produce_listings", ["status", "crop_id"], schema=SCHEMA
    )
    # Distance search runs ST_DWithin on geography(pickup_location): index that expression.
    op.create_index(
        "listings_pickup_gix",
        "produce_listings",
        [sa.text("geography(pickup_location)")],
        schema=SCHEMA,
        postgresql_using="gist",
    )

    op.create_table(
        "stock_reservations",
        sa.Column("order_id", sa.Uuid(), primary_key=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("released_at", TS, nullable=True),
        schema=SCHEMA,
    )
    op.create_table(
        "stock_movements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "listing_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.produce_listings.id"),
            nullable=False,
        ),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("delta", QTY, nullable=False),
        sa.Column("reason", sa.String(12), nullable=False),
        sa.Column("unit_price", MONEY, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_catalog_stock_movements_listing_id", "stock_movements", ["listing_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_catalog_stock_movements_order_id", "stock_movements", ["order_id"], schema=SCHEMA
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
    op.create_index("ix_catalog_outbox_type", "outbox", ["type"], schema=SCHEMA)
    op.create_index("ix_catalog_outbox_occurred_at", "outbox", ["occurred_at"], schema=SCHEMA)
    op.create_index("ix_catalog_outbox_published_at", "outbox", ["published_at"], schema=SCHEMA)
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("processed_at", TS, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )


def downgrade() -> None:
    for table in (
        "processed_events",
        "outbox",
        "stock_movements",
        "stock_reservations",
        "produce_listings",
        "farms",
        "crops",
    ):
        op.drop_table(table, schema=SCHEMA)
