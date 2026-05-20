"""make loop key nullable

Revision ID: o4p36k15l2m8
Revises: n3o25j04k1l7
Create Date: 2026-05-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "o4p36k15l2m8"
down_revision = "n3o25j04k1l7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE loops ALTER COLUMN key DROP NOT NULL"))


def downgrade() -> None:
    op.execute(sa.text("UPDATE loops SET key = '' WHERE key IS NULL"))
    op.execute(sa.text("ALTER TABLE loops ALTER COLUMN key SET NOT NULL"))
