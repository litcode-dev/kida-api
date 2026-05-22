"""add is_suspended to users

Revision ID: y4z36u15v2w7
Revises: x3y25t04u1v6
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa

revision = "y4z36u15v2w7"
down_revision = "x3y25t04u1v6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_suspended", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("users", "is_suspended")
