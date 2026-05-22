"""add suspension_reason to users

Revision ID: z5a47v26w3x8
Revises: y4z36u15v2w7
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa

revision = "z5a47v26w3x8"
down_revision = "y4z36u15v2w7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("suspension_reason", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "suspension_reason")
