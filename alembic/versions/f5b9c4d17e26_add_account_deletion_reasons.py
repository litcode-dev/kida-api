"""add account deletion reasons

Revision ID: f5b9c4d17e26
Revises: f4a8b3c06d15
Create Date: 2026-08-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM, UUID


revision = "f5b9c4d17e26"
down_revision = "f4a8b3c06d15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A table of its own rather than a column on account_deletion_audit: that
    # row is guaranteed to hold no personal data, and a free-text box is the
    # one field someone can put anything into. Nothing here points back at a
    # deletion or an account.
    #
    # The deletion_actor enum already exists (account_deletion_audit created
    # it), so it is referenced rather than declared again, which would fail as
    # a duplicate type. create_type is a postgres-dialect option, so this has
    # to be the dialect's ENUM and not sa.Enum, which silently ignores it.
    actor = ENUM(
        "user", "admin", "email_request", name="deletion_actor", create_type=False
    )
    op.create_table(
        "account_deletion_reasons",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", actor, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_account_deletion_reasons_created_at",
        "account_deletion_reasons",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_account_deletion_reasons_created_at",
        table_name="account_deletion_reasons",
    )
    op.drop_table("account_deletion_reasons")
