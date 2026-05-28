"""add time signature to loops

Revision ID: r7s49t28u5v1
Revises: a6b58w37x4y9
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa


revision = "r7s49t28u5v1"
down_revision = "a6b58w37x4y9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "loops",
        sa.Column(
            "time_signature",
            sa.String(length=16),
            nullable=False,
            server_default="4/4",
        ),
    )


def downgrade() -> None:
    op.drop_column("loops", "time_signature")
