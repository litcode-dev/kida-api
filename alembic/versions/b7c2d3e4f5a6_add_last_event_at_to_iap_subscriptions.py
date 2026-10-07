"""remember the last RevenueCat event applied to an entitlement

RevenueCat does not guarantee webhook delivery order. Storing the
event_timestamp_ms of the last event applied lets an older event that arrives
late be skipped instead of overwriting newer state.

NULL means no timestamped webhook has been applied yet, which is what every
row looks like before this migration; the first event after it is applied.

Revision ID: b7c2d3e4f5a6
Revises: a6b1c2d3e4f5
Create Date: 2026-10-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "b7c2d3e4f5a6"
down_revision = "a6b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE iap_subscriptions ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ"
    ))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE iap_subscriptions DROP COLUMN IF EXISTS last_event_at"))
