"""add African praise, drill, seben, reggae and neighbours to the genre enum

Revision ID: o1p92k73l6m4
Revises: n0o81j62k5l3
Create Date: 2026-08-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "o1p92k73l6m4"
down_revision = "n0o81j62k5l3"
branch_labels = None
depends_on = None

# Postgres labels are the Python member *names*; the API speaks the values
# ("African Praise"). Keep this list in step with app.models.loop.Genre.
NEW_GENRES = [
    "african_praise",
    "drill",
    "seben",
    "reggae",
    "highlife",
    "soukous",
    "rumba",
    "afro_pop",
    "hip_hop",
    "rnb",
]

# What each new genre collapses to if this migration is rolled back, since the
# old type has no room for them. Chosen as the nearest surviving neighbour
# rather than dumping everything into one bucket.
DOWNGRADE_FALLBACK = {
    "african_praise": "gospel",
    "drill": "trap",
    "seben": "afrobeat",
    "reggae": "dancehall",
    "highlife": "afrobeat",
    "soukous": "afrobeat",
    "rumba": "afrobeat",
    "afro_pop": "afrobeat",
    "hip_hop": "boom_bap",
    "rnb": "afrobeat",
}

OLD_GENRES = [
    "afrobeat", "amapiano", "trap", "boom_bap", "lo_fi", "gospel",
    "afrobeat_worship", "contemporary_worship", "dancehall", "afrohouse",
    "highlife_gospel",
]

GENRE_TABLES = ["loops", "stem_packs"]


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is allowed inside a transaction from PG12 on, as
    # long as the new label is not used in that same transaction. Nothing here
    # writes a row with a new genre, so this is safe.
    for label in NEW_GENRES:
        op.execute(sa.text(f"ALTER TYPE genre ADD VALUE IF NOT EXISTS '{label}'"))


def downgrade() -> None:
    # Postgres cannot drop a label from an enum, so the type is rebuilt without
    # the new ones. Rows using them are remapped first, otherwise the cast fails.
    for table in GENRE_TABLES:
        for label, fallback in DOWNGRADE_FALLBACK.items():
            op.execute(sa.text(
                f"UPDATE {table} SET genre = '{fallback}' WHERE genre = '{label}'"
            ))

    old_list = ", ".join(f"'{g}'" for g in OLD_GENRES)
    op.execute(sa.text("ALTER TYPE genre RENAME TO genre_old"))
    op.execute(sa.text(f"CREATE TYPE genre AS ENUM ({old_list})"))
    for table in GENRE_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN genre TYPE genre "
            "USING genre::text::genre"
        ))
    op.execute(sa.text("DROP TYPE genre_old"))
