"""add hausa groove and ariaria to the genre enum

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

GENRE_TABLES = ["loops", "stem_packs"]

NEW_GENRES = ["hausa_groove", "ariaria"]

# Where rows land if this is rolled back. Both are Nigerian, so unlike the
# jazz/blues/country batch they do have near neighbours: Ariaria is named for
# the market in Aba and sits closest to highlife, while hausa_groove has no
# northern counterpart in the list and falls back to the broadest Nigerian
# genre there is.
DOWNGRADE_FALLBACK = {"hausa_groove": "afrobeat", "ariaria": "highlife"}

# Every label the type holds *before* this revision, in order — the downgrade
# has to rebuild the type to remove one.
GENRES_BEFORE = [
    "afrobeat", "amapiano", "trap", "boom_bap", "lo_fi", "gospel",
    "afrobeat_worship", "contemporary_worship", "dancehall", "afrohouse",
    "highlife_gospel", "african_praise", "drill", "seben", "reggae",
    "highlife", "soukous", "rumba", "afro_pop", "hip_hop", "rnb",
    "kompa", "fuji", "jazz", "blues", "country",
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
