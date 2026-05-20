"""make loop key nullable

Revision ID: r7s69n48o5p0
Revises: o4p36k15l2m8
Create Date: 2026-05-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "r7s69n48o5p0"
down_revision = "o4p36k15l2m8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE loops ALTER COLUMN key DROP NOT NULL"))


def downgrade() -> None:
    op.execute(sa.text("UPDATE loops SET key = '' WHERE key IS NULL"))
    op.execute(sa.text("ALTER TABLE loops ALTER COLUMN key SET NOT NULL"))
