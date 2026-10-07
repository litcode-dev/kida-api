"""add desktop_apps and paid app downloads

Admins publish installers (name, OS, R2/S3/https location, free or paid with a
price in NGN or USD). A download request can now name one of them; a paid one
waits on a payment before its 3-day link is emailed, so expires_at becomes
nullable until then. Existing rows were all emailed straight away and keep the
'fulfilled' default.

Revision ID: c8d3e4f5a6b7
Revises: b7c2d3e4f5a6
Create Date: 2026-10-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "c8d3e4f5a6b7"
down_revision = "b7c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "desktop_apps",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("os", sa.String(16), nullable=False),
        sa.Column("file_url", sa.String(1024), nullable=False),
        sa.Column("is_paid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "uq_desktop_apps_name_os", "desktop_apps", [sa.text("lower(name)"), "os"], unique=True
    )

    op.add_column("app_download_requests", sa.Column("app_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_app_download_requests_app_id", "app_download_requests", "desktop_apps",
        ["app_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_app_download_requests_app_id", "app_download_requests", ["app_id"])
    op.add_column("app_download_requests", sa.Column("app_name", sa.String(120), nullable=True))
    op.add_column(
        "app_download_requests",
        sa.Column("status", sa.String(20), nullable=False, server_default="fulfilled"),
    )
    op.add_column("app_download_requests", sa.Column("amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("app_download_requests", sa.Column("currency", sa.String(3), nullable=True))
    op.add_column("app_download_requests", sa.Column("payment_provider", sa.String(20), nullable=True))
    op.add_column("app_download_requests", sa.Column("payment_reference", sa.String(255), nullable=True))
    op.create_unique_constraint(
        "uq_app_download_requests_payment_reference", "app_download_requests", ["payment_reference"]
    )
    op.add_column("app_download_requests", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("app_download_requests", "expires_at", nullable=True)


def downgrade() -> None:
    # Unpaid requests have no expiry and no link worth keeping.
    op.execute(sa.text("DELETE FROM app_download_requests WHERE expires_at IS NULL"))
    op.alter_column("app_download_requests", "expires_at", nullable=False)
    op.drop_column("app_download_requests", "paid_at")
    op.drop_constraint(
        "uq_app_download_requests_payment_reference", "app_download_requests", type_="unique"
    )
    op.drop_column("app_download_requests", "payment_reference")
    op.drop_column("app_download_requests", "payment_provider")
    op.drop_column("app_download_requests", "currency")
    op.drop_column("app_download_requests", "amount")
    op.drop_column("app_download_requests", "status")
    op.drop_column("app_download_requests", "app_name")
    op.drop_index("ix_app_download_requests_app_id", table_name="app_download_requests")
    op.drop_constraint("fk_app_download_requests_app_id", "app_download_requests", type_="foreignkey")
    op.drop_column("app_download_requests", "app_id")
    op.drop_index("uq_desktop_apps_name_os", table_name="desktop_apps")
    op.drop_table("desktop_apps")
