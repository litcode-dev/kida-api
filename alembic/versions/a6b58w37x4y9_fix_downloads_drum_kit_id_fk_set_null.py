"""fix downloads drum_kit_id fk set null on delete

Revision ID: a6b58w37x4y9
Revises: z5a47v26w3x8
Create Date: 2026-05-26
"""
from alembic import op

revision = "a6b58w37x4y9"
down_revision = "z5a47v26w3x8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("downloads_drum_kit_id_fkey", "downloads", type_="foreignkey")
    op.create_foreign_key(
        "downloads_drum_kit_id_fkey",
        "downloads",
        "drum_kits",
        ["drum_kit_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("downloads_drum_kit_id_fkey", "downloads", type_="foreignkey")
    op.create_foreign_key(
        "downloads_drum_kit_id_fkey",
        "downloads",
        "drum_kits",
        ["drum_kit_id"],
        ["id"],
    )
