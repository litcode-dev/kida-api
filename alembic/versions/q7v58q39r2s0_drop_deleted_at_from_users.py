"""drop deleted_at from users

Account deletion no longer has a grace period. DELETE /auth/me and the confirmed
public deletion link both hard-delete straight away, so no row is ever left in a
pending state and the column has nothing left to hold.

Accounts still soft-deleted when this deploys are the awkward part. Dropping the
column under them would silently put them back into service — people who asked
to leave, restored by a schema change. Deleting them in SQL here is no better:
their stored files, their RevenueCat subscriber and their OneSignal record all
live outside this database, and a DELETE statement reaches none of them.

So this refuses to run while any remain, and

    python -m scripts.purge_pending_deletions

drains them first through the real deletion path, which does reach all three.

Revision ID: q7v58q39r2s0
Revises: p6u47p28q1r9
Create Date: 2026-08-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "q7v58q39r2s0"
down_revision = "p6u47p28q1r9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    pending = conn.execute(
        sa.text("SELECT count(*) FROM users WHERE deleted_at IS NOT NULL")
    ).scalar_one()
    if pending:
        raise RuntimeError(
            f"{pending} account(s) are still soft-deleted. Dropping the column "
            "now would restore them. Run `python -m scripts.purge_pending_deletions` "
            "to finish those deletions properly, then re-run this migration."
        )

    op.drop_index("ix_users_deleted_at", table_name="users")
    op.drop_column("users", "deleted_at")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_deleted_at", "users", ["deleted_at"])
