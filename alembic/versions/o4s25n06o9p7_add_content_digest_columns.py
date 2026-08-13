"""add announced_at to content tables and published_at to stem packs

Backs the new-content digest: one email a day listing everything that went
live, instead of one email per item to every recipient. announced_at is what
makes an item appear in exactly one digest.

Existing content is stamped as already announced. Without that, the first
digest after deploy would email the entire back catalogue to every subscriber.

Revision ID: o4s25n06o9p7
Revises: o3r14m95n8o6
Create Date: 2026-08-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "o4s25n06o9p7"
down_revision = "o3r14m95n8o6"
branch_labels = None
depends_on = None

TABLES = ["loops", "drones", "drum_kits", "stem_packs"]


def upgrade() -> None:
    for table in TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS announced_at TIMESTAMPTZ"
        ))
        op.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_announced_at "
            f"ON {table} (announced_at)"
        ))
        # Everything that already exists counts as announced — it was emailed
        # per item under the old scheme, and a digest of the back catalogue
        # would be both wrong and expensive.
        op.execute(sa.text(
            f"UPDATE {table} SET announced_at = now() WHERE announced_at IS NULL"
        ))

    op.execute(sa.text(
        "ALTER TABLE stem_packs ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_stem_packs_published_at "
        "ON stem_packs (published_at)"
    ))
    # Packs that predate the publish flag are treated as published, matching
    # the announced_at backfill above.
    op.execute(sa.text(
        "UPDATE stem_packs SET published_at = created_at WHERE published_at IS NULL"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_stem_packs_published_at"))
    op.execute(sa.text("ALTER TABLE stem_packs DROP COLUMN IF EXISTS published_at"))

    for table in TABLES:
        op.execute(sa.text(f"DROP INDEX IF EXISTS ix_{table}_announced_at"))
        op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS announced_at"))
