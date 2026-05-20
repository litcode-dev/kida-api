"""add price to drum_kits

Revision ID: q6r58m37n4o9
Revises: p5q47l26m3n8
Create Date: 2026-05-20
"""
from alembic import op
import sqlalchemy as sa

revision = "q6r58m37n4o9"
down_revision = "p5q47l26m3n8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("drum_kits", sa.Column("price", sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("drum_kits", "price")
