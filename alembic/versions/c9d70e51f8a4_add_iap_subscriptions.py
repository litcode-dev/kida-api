"""add iap subscriptions

Revision ID: c9d70e51f8a4
Revises: b8c69d40e5f2
Create Date: 2026-07-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c9d70e51f8a4"
down_revision = "b8c69d40e5f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE iapplatform AS ENUM ('ios', 'android');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE iapsubscriptionstatus AS ENUM ('active', 'grace', 'expired', 'canceled', 'revoked');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$
    """))

    op.create_table(
        "iap_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "platform",
            postgresql.ENUM("ios", "android", name="iapplatform", create_type=False),
            nullable=False,
        ),
        sa.Column("product_id", sa.String(length=255), nullable=False),
        sa.Column("store_transaction_id", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "grace", "expired", "canceled", "revoked",
                name="iapsubscriptionstatus", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_receipt", sa.Text(), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_iap_subscriptions_user_id"),
        sa.UniqueConstraint("store_transaction_id", name="uq_iap_subscriptions_store_transaction_id"),
    )
    op.create_index("ix_iap_subscriptions_user_id", "iap_subscriptions", ["user_id"])
    op.create_index(
        "ix_iap_subscriptions_store_transaction_id", "iap_subscriptions", ["store_transaction_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_iap_subscriptions_store_transaction_id", table_name="iap_subscriptions")
    op.drop_index("ix_iap_subscriptions_user_id", table_name="iap_subscriptions")
    op.drop_table("iap_subscriptions")
    op.execute(sa.text("DROP TYPE IF EXISTS iapsubscriptionstatus"))
    op.execute(sa.text("DROP TYPE IF EXISTS iapplatform"))
