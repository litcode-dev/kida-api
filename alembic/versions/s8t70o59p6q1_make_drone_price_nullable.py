"""make drone price nullable

Revision ID: s8t70o59p6q1
Revises: q6r58m37n4o9
Create Date: 2026-05-20

"""
from alembic import op

revision = "s8t70o59p6q1"
down_revision = "q6r58m37n4o9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("drone_pads", "price", nullable=True)


def downgrade() -> None:
    op.execute("UPDATE drone_pads SET price = 0 WHERE price IS NULL")
    op.alter_column("drone_pads", "price", nullable=False)
