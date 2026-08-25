"""add salsa to the genre enum

Revision ID: f2d6e1f84a93
Revises: f1c5d0e73f82
Create Date: 2026-08-25
"""
from alembic import op
import sqlalchemy as sa


revision = "f2d6e1f84a93"
down_revision = "f1c5d0e73f82"
branch_labels = None
depends_on = None


GENRE_TABLES = ["loops", "stem_packs"]
GENRES_BEFORE = [
    "afrobeat", "amapiano", "trap", "boom_bap", "lo_fi", "gospel",
    "afrobeat_worship", "contemporary_worship", "dancehall", "afrohouse",
    "highlife_gospel", "african_praise", "drill", "seben", "reggae",
    "highlife", "soukous", "rumba", "afro_pop", "hip_hop", "rnb",
    "kompa", "fuji", "jazz", "blues", "country",
]


def upgrade() -> None:
    op.execute(sa.text("ALTER TYPE genre ADD VALUE IF NOT EXISTS 'salsa'"))


def downgrade() -> None:
    # PostgreSQL cannot remove enum labels. Existing Salsa content becomes
    # Rumba, its closest remaining Latin-dance genre, before rebuilding.
    for table in GENRE_TABLES:
        op.execute(sa.text(f"UPDATE {table} SET genre = 'rumba' WHERE genre = 'salsa'"))

    kept = ", ".join(f"'{genre}'" for genre in GENRES_BEFORE)
    op.execute(sa.text("ALTER TYPE genre RENAME TO genre_old"))
    op.execute(sa.text(f"CREATE TYPE genre AS ENUM ({kept})"))
    for table in GENRE_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN genre TYPE genre "
            "USING genre::text::genre"
        ))
    op.execute(sa.text("DROP TYPE genre_old"))
