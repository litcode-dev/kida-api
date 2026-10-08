"""add position to desktop_apps

Apps can now be put in any order by an admin. Existing apps keep the order
they were listed in until now, alphabetical by name.

Revision ID: e0f5a6b7c8d9
Revises: d9e4f5a6b7c8
Create Date: 2026-10-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "e0f5a6b7c8d9"
down_revision = "d9e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "desktop_apps",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(sa.text(
        """
        UPDATE desktop_apps d SET position = ranked.rn
        FROM (
            SELECT id, row_number() OVER (ORDER BY lower(name)) - 1 AS rn
            FROM desktop_apps
        ) ranked
        WHERE d.id = ranked.id
        """
    ))


def downgrade() -> None:
    op.drop_column("desktop_apps", "position")
