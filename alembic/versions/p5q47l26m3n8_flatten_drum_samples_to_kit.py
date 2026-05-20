"""flatten drum samples to kit — remove category layer

Revision ID: p5q47l26m3n8
Revises: o4p36k15l2m8
Create Date: 2026-05-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "p5q47l26m3n8"
down_revision = "o4p36k15l2m8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add drum_kit_id directly on drum_samples
    op.add_column(
        "drum_samples",
        sa.Column("drum_kit_id", UUID(as_uuid=True), nullable=True),
    )

    # Backfill drum_kit_id from the category join
    op.execute("""
        UPDATE drum_samples ds
        SET drum_kit_id = dkc.drum_kit_id
        FROM drum_kit_categories dkc
        WHERE ds.category_id = dkc.id
    """)

    # Make drum_kit_id NOT NULL and add FK now that it's populated
    op.alter_column("drum_samples", "drum_kit_id", nullable=False)
    op.create_foreign_key(
        "fk_drum_samples_drum_kit_id",
        "drum_samples", "drum_kits",
        ["drum_kit_id"], ["id"],
        ondelete="CASCADE",
    )

    # Drop old category FK and column
    op.drop_constraint("drum_samples_category_id_fkey", "drum_samples", type_="foreignkey")
    op.drop_column("drum_samples", "category_id")

    # Drop the categories table
    op.drop_table("drum_kit_categories")


def downgrade() -> None:
    op.create_table(
        "drum_kit_categories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("drum_kit_id", UUID(as_uuid=True), sa.ForeignKey("drum_kits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.add_column(
        "drum_samples",
        sa.Column("category_id", UUID(as_uuid=True), nullable=True),
    )

    # Downgrade cannot recover original categories — samples will have NULL category_id
    op.drop_constraint("fk_drum_samples_drum_kit_id", "drum_samples", type_="foreignkey")
    op.drop_column("drum_samples", "drum_kit_id")
