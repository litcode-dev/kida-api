"""add loop requests

Revision ID: f0c4b9d62e71
Revises: a04cd7bbb94d
Create Date: 2026-08-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "f0c4b9d62e71"
down_revision = "a04cd7bbb94d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loop_requests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("artist_name", sa.String(255), nullable=False),
        sa.Column("song_title", sa.String(255), nullable=False),
        sa.Column("reference_link", sa.String(2048), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_loop_requests_user_id", "loop_requests", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_loop_requests_user_id", table_name="loop_requests")
    op.drop_table("loop_requests")
