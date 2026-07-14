"""add price sync fields to sellable items

Revision ID: b8c69d40e5f2
Revises: a7b58c39d4e1
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa

revision = "b8c69d40e5f2"
down_revision = "a7b58c39d4e1"
branch_labels = None
depends_on = None

SELLABLE_TABLES = ("loops", "drum_kits", "drones")


def upgrade() -> None:
    for table in SELLABLE_TABLES:
        op.add_column(table, sa.Column("desired_price_usd", sa.Numeric(10, 2), nullable=True))
        op.add_column(
            table,
            sa.Column("price_sync_state", sa.String(30), nullable=False, server_default="pending"),
        )
        op.add_column(table, sa.Column("price_synced_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("price_sync_error", sa.Text(), nullable=True))
        op.create_index(
            f"ix_{table}_store_product_id", table, ["store_product_id"], unique=True
        )


def downgrade() -> None:
    for table in reversed(SELLABLE_TABLES):
        op.drop_index(f"ix_{table}_store_product_id", table_name=table)
        op.drop_column(table, "price_sync_error")
        op.drop_column(table, "price_synced_at")
        op.drop_column(table, "price_sync_state")
        op.drop_column(table, "desired_price_usd")
