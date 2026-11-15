"""drop position from loops

The admin-arranged loop order (f1a6b7c8d9e0) was withdrawn. The column is
dropped by a migration of its own rather than by deleting that one, so a
database already upgraded past it can still find its current revision.
IF EXISTS keeps this safe either way.

Revision ID: g2b7c8d9e0f1
Revises: f1a6b7c8d9e0
Create Date: 2026-11-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "g2b7c8d9e0f1"
down_revision = "f1a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_loops_position")
    op.execute("ALTER TABLE loops DROP COLUMN IF EXISTS position")


def downgrade() -> None:
    op.add_column("loops", sa.Column("position", sa.Integer(), nullable=True))
    op.create_index("ix_loops_position", "loops", ["position"])
