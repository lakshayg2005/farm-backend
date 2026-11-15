"""identity: users, roles, refresh tokens, OTP codes, outbox

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

SCHEMA = "identity"
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("phone", sa.String(16), nullable=False, unique=True),
        sa.Column("email", sa.String(254), nullable=True, unique=True),
        sa.Column("password_hash", sa.String(100), nullable=False),
        sa.Column("phone_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("farmer_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("home_lat", sa.Float(), nullable=True),
        sa.Column("home_lng", sa.Float(), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_table(
        "user_roles",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.String(20), primary_key=True),
        schema=SCHEMA,
    )
    op.create_index("ix_user_roles_role", "user_roles", ["role"], schema=SCHEMA)
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"], schema=SCHEMA)
    op.create_table(
        "otp_codes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("phone", sa.String(16), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("consumed_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_otp_codes_phone", "otp_codes", ["phone"], schema=SCHEMA)
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
    op.create_index("ix_outbox_type", "outbox", ["type"], schema=SCHEMA)
    op.create_index("ix_outbox_occurred_at", "outbox", ["occurred_at"], schema=SCHEMA)
    op.create_index("ix_outbox_published_at", "outbox", ["published_at"], schema=SCHEMA)
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
        "otp_codes",
        "refresh_tokens",
        "user_roles",
        "users",
    ):
        op.drop_table(table, schema=SCHEMA)
