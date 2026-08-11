"""add monthly_download_usage

Backs the monthly download allowance (loops, drones, drum kits). One row per
distinct item taken in a calendar month; the downloads table cannot be used for
this because cleanup_expired_downloads deletes those rows within the hour.

Revision ID: l8m69h40i3j1
Revises: k7l58g39h2i0
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "l8m69h40i3j1"
down_revision = "k7l58g39h2i0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monthly_download_usage",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column(
            "item_type",
            sa.Enum("loop", "drone", "drum_kit", name="monthlyquotatype"),
            nullable=False,
        ),
        sa.Column("item_id", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "user_id", "period", "item_type", "item_id",
            name="uq_monthly_download_usage_user_period_item",
        ),
    )
    op.create_index("ix_monthly_download_usage_user_id", "monthly_download_usage", ["user_id"])
    op.create_index(
        "ix_monthly_usage_user_period_type",
        "monthly_download_usage",
        ["user_id", "period", "item_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_monthly_usage_user_period_type", table_name="monthly_download_usage")
    op.drop_index("ix_monthly_download_usage_user_id", table_name="monthly_download_usage")
    op.drop_table("monthly_download_usage")
    op.execute("DROP TYPE IF EXISTS monthlyquotatype")
