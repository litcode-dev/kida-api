"""add super_admin to the userrole enum

Revision ID: j6k47f28g1h9
Revises: i5j36e17f0g8
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "j6k47f28g1h9"
down_revision = "i5j36e17f0g8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is allowed inside a transaction from PG12 on, as
    # long as the new label is not used in that same transaction. Nothing here
    # writes a super_admin row, so this is safe.
    op.execute(sa.text("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'super_admin'"))


def downgrade() -> None:
    # Postgres cannot drop a label from an enum, so the type is rebuilt without
    # it. Any super admin is demoted to admin first, otherwise the cast fails.
    op.execute(sa.text("UPDATE users SET role = 'admin' WHERE role = 'super_admin'"))
    op.execute(sa.text("ALTER TYPE userrole RENAME TO userrole_old"))
    op.execute(sa.text("CREATE TYPE userrole AS ENUM ('user', 'producer', 'admin')"))
    op.execute(sa.text(
        "ALTER TABLE users ALTER COLUMN role TYPE userrole "
        "USING role::text::userrole"
    ))
    op.execute(sa.text("DROP TYPE userrole_old"))
