"""restore position on loops

The admin-arranged loop order is back after being withdrawn (g2b7c8d9e0f1).
The column is re-added by a migration of its own rather than by deleting the
drop, so a database already upgraded past that drop can still find its
current revision. IF NOT EXISTS keeps this safe either way. Every loop comes
back unarranged (NULL): the order set before the withdrawal was dropped with
the column, so the catalogue reads newest first until it is arranged again.

Revision ID: h3c8d9e0f1a2
Revises: g2b7c8d9e0f1
Create Date: 2026-11-16 00:00:00.000000
"""
from alembic import op

revision = "h3c8d9e0f1a2"
down_revision = "g2b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE loops ADD COLUMN IF NOT EXISTS position INTEGER")
    op.execute("CREATE INDEX IF NOT EXISTS ix_loops_position ON loops (position)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_loops_position")
    op.execute("ALTER TABLE loops DROP COLUMN IF EXISTS position")
