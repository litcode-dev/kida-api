"""add is_verified to users

Revision ID: f2g03b84c7d5
Revises: e1f92a73b6c4
Create Date: 2026-08-02
"""
from alembic import op
import sqlalchemy as sa

revision = "f2g03b84c7d5"
down_revision = "e1f92a73b6c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New column defaults to false for freshly-registered accounts, which must
    # verify their email before logging in.
    op.add_column(
        "users",
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default="false"),
    )
    # Existing accounts predate email verification — treat them as verified so
    # they are not locked out of login.
    op.execute("UPDATE users SET is_verified = true")


def downgrade() -> None:
    op.drop_column("users", "is_verified")
