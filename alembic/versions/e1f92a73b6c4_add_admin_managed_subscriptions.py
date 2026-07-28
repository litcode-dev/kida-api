"""add admin-managed fields to iap subscriptions

Revision ID: e1f92a73b6c4
Revises: d0e81f62a9b5
Create Date: 2026-07-28 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e1f92a73b6c4"
down_revision = "d0e81f62a9b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE iapsubscriptionsource AS ENUM ('store', 'admin');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$
    """))

    op.add_column(
        "iap_subscriptions",
        sa.Column(
            "source",
            postgresql.ENUM("store", "admin", name="iapsubscriptionsource", create_type=False),
            nullable=False,
            server_default="store",
        ),
    )
    op.add_column(
        "iap_subscriptions",
        sa.Column("admin_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "iap_subscriptions",
        sa.Column("admin_note", sa.String(length=500), nullable=True),
    )
    # Admin grants have no store platform.
    op.alter_column("iap_subscriptions", "platform", nullable=True)


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE iap_subscriptions SET platform = 'android' WHERE platform IS NULL")
    )
    op.alter_column("iap_subscriptions", "platform", nullable=False)
    op.drop_column("iap_subscriptions", "admin_note")
    op.drop_column("iap_subscriptions", "admin_locked")
    op.drop_column("iap_subscriptions", "source")
    op.execute(sa.text("DROP TYPE IF EXISTS iapsubscriptionsource"))
