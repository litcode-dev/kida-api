"""add admin response to loop requests

Revision ID: f4a8b3c06d15
Revises: f3e7f2a95b04
Create Date: 2026-08-26
"""
from alembic import op
import sqlalchemy as sa


revision = "f4a8b3c06d15"
down_revision = "f3e7f2a95b04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # What the admin wants to say back — "we already have this one", "someone
    # asked for it last week". Nullable, because most moves need no explaining.
    op.add_column(
        "loop_requests",
        sa.Column("admin_response", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("loop_requests", "admin_response")
