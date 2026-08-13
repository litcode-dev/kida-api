"""add kompa and fuji to the genre enum

A separate revision from o1p92k73l6m4 rather than an edit to it: that one is
already pushed and may have run somewhere, and an applied revision is never
re-run, so labels added to it after the fact would silently never reach that
database.

Revision ID: o2q03l84m7n5
Revises: o1p92k73l6m4
Create Date: 2026-08-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "o2q03l84m7n5"
down_revision = "o1p92k73l6m4"
branch_labels = None
depends_on = None

GENRE_TABLES = ["loops", "stem_packs"]

NEW_GENRES = ["kompa", "fuji"]

# Where rows land if this revision is rolled back, since the earlier type has
# no room for them. Kompa keeps Caribbean company; fuji is Yoruba party music,
# so afrobeat is the nearest survivor.
DOWNGRADE_FALLBACK = {"kompa": "dancehall", "fuji": "afrobeat"}

# Every label the type holds *before* this revision, in order — the downgrade
# has to rebuild the type to remove one.
GENRES_BEFORE = [
    "afrobeat", "amapiano", "trap", "boom_bap", "lo_fi", "gospel",
    "afrobeat_worship", "contemporary_worship", "dancehall", "afrohouse",
    "highlife_gospel", "african_praise", "drill", "seben", "reggae",
    "highlife", "soukous", "rumba", "afro_pop", "hip_hop", "rnb",
]


def upgrade() -> None:
    for label in NEW_GENRES:
        op.execute(sa.text(f"ALTER TYPE genre ADD VALUE IF NOT EXISTS '{label}'"))


def downgrade() -> None:
    # Postgres cannot drop a label, so the type is rebuilt without these two.
    # Rows using them are remapped first, otherwise the cast fails.
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
