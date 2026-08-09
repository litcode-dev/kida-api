"""add deleted_at to users

Soft deletion for self-service account removal: DELETE /auth/me stamps
deleted_at and the purge job hard-deletes the row once the grace window
(ACCOUNT_DELETION_GRACE_DAYS) has passed.

Revision ID: h4i25d06e9f7
Revises: g3h14c95d8e6
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "h4i25d06e9f7"
down_revision = "g3h14c95d8e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The purge job scans for rows past the grace window, and every auth check
    # reads this column.
    op.create_index("ix_users_deleted_at", "users", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_users_deleted_at", table_name="users")
    op.drop_column("users", "deleted_at")
