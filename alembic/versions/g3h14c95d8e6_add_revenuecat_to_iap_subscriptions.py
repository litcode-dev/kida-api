"""add revenuecat source and app_user_id to iap_subscriptions

Revision ID: g3h14c95d8e6
Revises: f2g03b84c7d5
Create Date: 2026-08-03 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "g3h14c95d8e6"
down_revision = "f2g03b84c7d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New provenance value for entitlements sourced from RevenueCat. ALTER TYPE
    # ADD VALUE is transactional on PostgreSQL 12+; IF NOT EXISTS keeps it
    # idempotent across re-runs.
    op.execute(
        sa.text("ALTER TYPE iapsubscriptionsource ADD VALUE IF NOT EXISTS 'revenuecat'")
    )

    op.execute(
        sa.text(
            "ALTER TABLE iap_subscriptions ADD COLUMN IF NOT EXISTS app_user_id VARCHAR(255)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_iap_subscriptions_app_user_id "
            "ON iap_subscriptions (app_user_id)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_iap_subscriptions_app_user_id"))
    op.execute(sa.text("ALTER TABLE iap_subscriptions DROP COLUMN IF EXISTS app_user_id"))
    # PostgreSQL cannot drop a single enum value; the 'revenuecat' label is left
    # in place. It is harmless once no rows reference it.
