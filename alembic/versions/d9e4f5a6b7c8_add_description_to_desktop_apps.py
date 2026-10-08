"""add description to desktop_apps

Revision ID: d9e4f5a6b7c8
Revises: c8d3e4f5a6b7
Create Date: 2026-10-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "d9e4f5a6b7c8"
down_revision = "c8d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("desktop_apps", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("desktop_apps", "description")
