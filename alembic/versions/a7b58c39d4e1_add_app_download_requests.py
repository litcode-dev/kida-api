"""add app_download_requests table

Revision ID: a7b58c39d4e1
Revises: r7s49t28u5v1
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "a7b58c39d4e1"
down_revision = "r7s49t28u5v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_download_requests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("os", sa.String(16), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_app_download_requests_email", "app_download_requests", ["email"])
    op.create_index("ix_app_download_requests_token", "app_download_requests", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_app_download_requests_token", table_name="app_download_requests")
    op.drop_index("ix_app_download_requests_email", table_name="app_download_requests")
    op.drop_table("app_download_requests")
