"""index created_at on loops, drones and stem packs

Every catalogue listing orders by created_at DESC, and none of these three had
an index on it — so each page was a full sort of the table. drum_kits already
had one from the migration that created it; this brings the other three in
line.

Revision ID: o5t36o17p0q8
Revises: o4s25n06o9p7
Create Date: 2026-08-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "o5t36o17p0q8"
down_revision = "o4s25n06o9p7"
branch_labels = None
depends_on = None

TABLES = ["loops", "drones", "stem_packs"]


def upgrade() -> None:
    for table in TABLES:
        op.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_created_at ON {table} (created_at)"
        ))


def downgrade() -> None:
    for table in TABLES:
        op.execute(sa.text(f"DROP INDEX IF EXISTS ix_{table}_created_at"))
