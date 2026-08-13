"""add jazz, blues and country to the genre enum

Revision ID: o3r14m95n8o6
Revises: o2q03l84m7n5
Create Date: 2026-08-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "o3r14m95n8o6"
down_revision = "o2q03l84m7n5"
branch_labels = None
depends_on = None

GENRE_TABLES = ["loops", "stem_packs"]

NEW_GENRES = ["jazz", "blues", "country"]

# Where rows land if this is rolled back. None of the three has a close
# neighbour in the older list, so they fall back to lo_fi, the least wrong
# home for something that is neither African nor gospel.
DOWNGRADE_FALLBACK = {"jazz": "lo_fi", "blues": "lo_fi", "country": "lo_fi"}

# Every label the type holds *before* this revision, in order — the downgrade
# has to rebuild the type to remove one.
GENRES_BEFORE = [
    "afrobeat", "amapiano", "trap", "boom_bap", "lo_fi", "gospel",
    "afrobeat_worship", "contemporary_worship", "dancehall", "afrohouse",
    "highlife_gospel", "african_praise", "drill", "seben", "reggae",
    "highlife", "soukous", "rumba", "afro_pop", "hip_hop", "rnb",
    "kompa", "fuji",
]


def upgrade() -> None:
    for label in NEW_GENRES:
        op.execute(sa.text(f"ALTER TYPE genre ADD VALUE IF NOT EXISTS '{label}'"))


def downgrade() -> None:
    for table in GENRE_TABLES:
        for label, fallback in DOWNGRADE_FALLBACK.items():
            op.execute(sa.text(
                f"UPDATE {table} SET genre = '{fallback}' WHERE genre = '{label}'"
            ))

    kept = ", ".join(f"'{g}'" for g in GENRES_BEFORE)
    op.execute(sa.text("ALTER TYPE genre RENAME TO genre_old"))
    op.execute(sa.text(f"CREATE TYPE genre AS ENUM ({kept})"))
    for table in GENRE_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN genre TYPE genre "
            "USING genre::text::genre"
        ))
    op.execute(sa.text("DROP TYPE genre_old"))
