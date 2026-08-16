"""add account deletion audit and third-party propagation queue

The account row going away left no trace that it ever existed, so there was no
way to answer "prove you deleted me" except by grepping application logs — logs
which carried the address we had just promised to erase.

account_deletion_audit is the permanent, PII-free trail: the subject is an HMAC
of the user id, not the id or the address. account_deletion_propagation is the
transient work queue holding the identifiers RevenueCat and OneSignal know the
person by, deleted as soon as those calls succeed or are abandoned.

Revision ID: p6u47p28q1r9
Revises: o5t36o17p0q8
Create Date: 2026-08-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "p6u47p28q1r9"
down_revision = "o5t36o17p0q8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    deletion_actor = sa.Enum(
        "user", "admin", "email_request", name="deletion_actor"
    )
    propagation_status = sa.Enum(
        "pending", "done", "skipped", "failed", name="propagation_status"
    )

    op.create_table(
        "account_deletion_audit",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("actor", deletion_actor, nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "purged_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "revenuecat_status", propagation_status,
            server_default="pending", nullable=False,
        ),
        sa.Column(
            "onesignal_status", propagation_status,
            server_default="pending", nullable=False,
        ),
        sa.Column("last_error", sa.String(500), nullable=True),
    )
    # Support looks a deletion up by recomputing the hash from a user id.
    op.create_index(
        "ix_account_deletion_audit_subject_hash",
        "account_deletion_audit",
        ["subject_hash"],
    )

    op.create_table(
        "account_deletion_propagation",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "audit_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column("revenuecat_app_user_ids", sa.String(1000), nullable=True),
        sa.Column("onesignal_subscription_id", sa.String(255), nullable=True),
        sa.Column("onesignal_external_id", sa.String(255), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["audit_id"], ["account_deletion_audit.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_account_deletion_propagation_audit_id",
        "account_deletion_propagation",
        ["audit_id"],
    )
    # The retry sweep selects on this every few minutes.
    op.create_index(
        "ix_account_deletion_propagation_next_attempt_at",
        "account_deletion_propagation",
        ["next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_account_deletion_propagation_next_attempt_at",
        table_name="account_deletion_propagation",
    )
    op.drop_index(
        "ix_account_deletion_propagation_audit_id",
        table_name="account_deletion_propagation",
    )
    op.drop_table("account_deletion_propagation")
    op.drop_index(
        "ix_account_deletion_audit_subject_hash", table_name="account_deletion_audit"
    )
    op.drop_table("account_deletion_audit")
    sa.Enum(name="propagation_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="deletion_actor").drop(op.get_bind(), checkfirst=True)
