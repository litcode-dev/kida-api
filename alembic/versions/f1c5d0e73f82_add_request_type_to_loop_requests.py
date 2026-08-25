"""add request type to loop requests

Revision ID: f1c5d0e73f82
Revises: f0c4b9d62e71
Create Date: 2026-08-25
"""
from alembic import op
import sqlalchemy as sa


revision = "f1c5d0e73f82"
down_revision = "f0c4b9d62e71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The default makes this safe if requests were submitted before the field
    # was introduced. New API requests must still supply an explicit type.
    op.add_column(
        "loop_requests",
        sa.Column("request_type", sa.String(16), nullable=False, server_default="loop"),
    )
    op.create_check_constraint(
        "ck_loop_requests_request_type",
        "loop_requests",
        "request_type IN ('loop', 'stems')",
    )
    op.alter_column("loop_requests", "request_type", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_loop_requests_request_type", "loop_requests", type_="check")
    op.drop_column("loop_requests", "request_type")
