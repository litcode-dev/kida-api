"""add drone_pad_id to downloads

Revision ID: v1w03r82s9t4
Revises: u0v92q71r8s3
Create Date: 2026-05-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "v1w03r82s9t4"
down_revision = "u0v92q71r8s3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "downloads",
        sa.Column("drone_pad_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "downloads_drone_pad_id_fkey",
        "downloads",
        "drone_pads",
        ["drone_pad_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("downloads_drone_pad_id_fkey", "downloads", type_="foreignkey")
    op.drop_column("downloads", "drone_pad_id")
