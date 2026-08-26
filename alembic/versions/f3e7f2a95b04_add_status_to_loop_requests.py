"""add status to loop requests

Revision ID: f3e7f2a95b04
Revises: f2d6e1f84a93
Create Date: 2026-08-26
"""
from alembic import op
import sqlalchemy as sa


revision = "f3e7f2a95b04"
down_revision = "f2d6e1f84a93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Everything submitted before this migration is unworked by definition, so
    # the default backfills them as "new" rather than leaving the column null.
    op.add_column(
        "loop_requests",
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
    )
    op.add_column(
        "loop_requests",
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_loop_requests_status",
        "loop_requests",
        "status IN ('new', 'in_progress', 'fulfilled', 'declined')",
    )
    # The queue view: the open requests, oldest first within a status. Kept as
    # one composite index because status alone is too coarse to be selective.
    op.create_index(
        "ix_loop_requests_status_created_at",
        "loop_requests",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_loop_requests_status_created_at", table_name="loop_requests")
    op.drop_constraint("ck_loop_requests_status", "loop_requests", type_="check")
    op.drop_column("loop_requests", "status_changed_at")
    op.drop_column("loop_requests", "status")
