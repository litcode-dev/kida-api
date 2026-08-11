"""add download_extra_credits to users

Paid-for downloads beyond the monthly allowance, spent one per item once the
month's quota for that type is used up. Mirrors ai_extra_credits.

Revision ID: m9n70i51j4k2
Revises: l8m69h40i3j1
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "m9n70i51j4k2"
down_revision = "l8m69h40i3j1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "download_extra_credits",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "download_extra_credits")
