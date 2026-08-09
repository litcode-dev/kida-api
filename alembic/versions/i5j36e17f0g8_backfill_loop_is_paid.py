"""backfill loops.is_paid to the inverse of is_free

is_paid is meant to be the inverse of is_free — create_loop has always set it
that way, but update_loop left it untouched until the fix in "Stop serving loops
whose audio is mid-reprocessing". Any loop toggled between free and paid before
that still carries the stale flag, which clients read to decide whether to gate
a download.

Rows where the two already disagree correctly (is_paid != is_free) are left
alone; only the contradictory ones are corrected.

Revision ID: i5j36e17f0g8
Revises: h4i25d06e9f7
Create Date: 2026-08-09
"""
from alembic import op

revision = "i5j36e17f0g8"
down_revision = "h4i25d06e9f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE loops SET is_paid = NOT is_free WHERE is_paid = is_free")


def downgrade() -> None:
    # The previous values were wrong by definition and were not recorded, so
    # there is nothing meaningful to restore.
    pass
