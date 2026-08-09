"""promote kida.audio@gmail.com to super_admin

Bootstraps the first super admin. The role cannot be granted through the API by
design (see the super_admin change), so the first one has to come from a
migration.

No-op when no account holds that address — the account has to exist first.

Revision ID: k7l58g39h2i0
Revises: j6k47f28g1h9
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "k7l58g39h2i0"
down_revision = "j6k47f28g1h9"
branch_labels = None
depends_on = None

SUPER_ADMIN_EMAIL = "kida.audio@gmail.com"


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE users SET role = 'super_admin' WHERE lower(email) = :email"
        ).bindparams(email=SUPER_ADMIN_EMAIL)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE users SET role = 'admin' "
            "WHERE lower(email) = :email AND role = 'super_admin'"
        ).bindparams(email=SUPER_ADMIN_EMAIL)
    )
