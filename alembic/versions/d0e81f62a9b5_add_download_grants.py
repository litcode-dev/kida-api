"""add download grants

Revision ID: d0e81f62a9b5
Revises: c9d70e51f8a4
Create Date: 2026-07-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d0e81f62a9b5"
down_revision = "c9d70e51f8a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE downloadgranttype AS ENUM ('loop', 'drum_kit', 'drone_pad', 'drone_group');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$
    """))

    op.create_table(
        "download_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "item_type",
            postgresql.ENUM(
                "loop", "drum_kit", "drone_pad", "drone_group",
                name="downloadgranttype", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("item_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "item_type", "item_id", name="uq_download_grants_user_item"),
    )
    op.create_index(op.f("ix_download_grants_user_id"), "download_grants", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_download_grants_user_id"), table_name="download_grants")
    op.drop_table("download_grants")
    op.execute(sa.text("DROP TYPE IF EXISTS downloadgranttype"))
