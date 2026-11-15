"""add position to loops

Admins can now arrange loops in any order, which listings follow with
`sort=custom`. Every existing loop starts unarranged (NULL), so the arranged
order begins empty and the catalogue reads newest first until it is set.

Revision ID: f1a6b7c8d9e0
Revises: e0f5a6b7c8d9
Create Date: 2026-11-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "f1a6b7c8d9e0"
down_revision = "e0f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("loops", sa.Column("position", sa.Integer(), nullable=True))
    op.create_index("ix_loops_position", "loops", ["position"])


def downgrade() -> None:
    op.drop_index("ix_loops_position", table_name="loops")
    op.drop_column("loops", "position")
