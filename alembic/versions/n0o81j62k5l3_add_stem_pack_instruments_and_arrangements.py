"""add stem pack instruments, song parts and arrangements

Gives a stem pack a shape: every stem now names the instrument it carries, and
a pack is either long-form (one continuous stem per instrument) or a breakdown
(song parts, each holding one stem per instrument) that arrangements stitch
back into a full song.

Existing packs and stems predate the distinction, so they become long-form with
their stems filed under "others" — nothing is dropped and nothing has to be
re-uploaded.

Revision ID: n0o81j62k5l3
Revises: m9n70i51j4k2
Create Date: 2026-08-12 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "n0o81j62k5l3"
down_revision = "m9n70i51j4k2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE stem_pack_type AS ENUM ('long_form', 'breakdown');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE stem_instrument AS ENUM (
                'drums', 'piano', 'guitar', 'bass_guitar',
                'vocal', 'cue', 'metronome', 'others'
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE song_section AS ENUM (
                'intro', 'verse', 'pre_chorus', 'chorus', 'hook', 'refrain',
                'bridge', 'vamp', 'interlude', 'instrumental', 'solo', 'outro', 'other'
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """))

    op.execute(sa.text("""
        ALTER TABLE stem_packs
        ADD COLUMN IF NOT EXISTS pack_type stem_pack_type NOT NULL DEFAULT 'long_form'
    """))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_stem_packs_pack_type ON stem_packs (pack_type)"
    ))

    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS stem_parts (
            id UUID PRIMARY KEY,
            stem_pack_id UUID NOT NULL REFERENCES stem_packs(id) ON DELETE CASCADE,
            section song_section NOT NULL,
            name VARCHAR(100) NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            bars INTEGER,
            created_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_stem_parts_pack_name UNIQUE (stem_pack_id, name)
        )
    """))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_stem_parts_stem_pack_id ON stem_parts (stem_pack_id)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_stem_parts_pack_position "
        "ON stem_parts (stem_pack_id, position)"
    ))

    # Stems uploaded before this migration are whole-song takes with no
    # instrument recorded, so they land in long-form packs as "others".
    op.execute(sa.text("""
        ALTER TABLE stems
        ADD COLUMN IF NOT EXISTS instrument stem_instrument NOT NULL DEFAULT 'others'
    """))
    op.execute(sa.text("""
        ALTER TABLE stems
        ADD COLUMN IF NOT EXISTS part_id UUID REFERENCES stem_parts(id) ON DELETE CASCADE
    """))
    op.execute(sa.text("""
        ALTER TABLE stems
        ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'processing'
    """))
    # Rows already carrying audio are finished, whatever the new default says.
    op.execute(sa.text(
        "UPDATE stems SET status = 'ready' WHERE file_s3_key IS NOT NULL"
    ))
    op.execute(sa.text("ALTER TABLE stems ALTER COLUMN duration SET DEFAULT 0"))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_stems_instrument ON stems (instrument)"
    ))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_stems_part_id ON stems (part_id)"))

    # One stem per instrument per part…
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE stems ADD CONSTRAINT uq_stems_part_instrument
                UNIQUE (part_id, instrument);
        EXCEPTION WHEN duplicate_table THEN NULL; WHEN duplicate_object THEN NULL; END $$
    """))
    # …and one per instrument per pack for long-form stems, where part_id is
    # NULL and a plain unique constraint would not bite. Pre-existing packs can
    # hold several "others" stems, so the index is only created when it can be:
    # those packs keep working, they simply cannot gain a duplicate later.
    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE UNIQUE INDEX IF NOT EXISTS uq_stems_pack_instrument_long_form
                ON stems (stem_pack_id, instrument) WHERE part_id IS NULL;
        EXCEPTION WHEN unique_violation THEN
            RAISE NOTICE 'skipping uq_stems_pack_instrument_long_form: existing duplicates';
        END $$
    """))

    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS stem_arrangements (
            id UUID PRIMARY KEY,
            stem_pack_id UUID NOT NULL REFERENCES stem_packs(id) ON DELETE CASCADE,
            name VARCHAR(150) NOT NULL,
            description TEXT,
            is_default BOOLEAN NOT NULL DEFAULT false,
            status VARCHAR(20) NOT NULL DEFAULT 'draft',
            duration INTEGER NOT NULL DEFAULT 0,
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ DEFAULT now(),
            rendered_at TIMESTAMPTZ,
            CONSTRAINT uq_stem_arrangements_pack_name UNIQUE (stem_pack_id, name)
        )
    """))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_stem_arrangements_stem_pack_id "
        "ON stem_arrangements (stem_pack_id)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_stem_arrangements_created_by "
        "ON stem_arrangements (created_by)"
    ))

    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS stem_arrangement_items (
            id UUID PRIMARY KEY,
            arrangement_id UUID NOT NULL REFERENCES stem_arrangements(id) ON DELETE CASCADE,
            part_id UUID NOT NULL REFERENCES stem_parts(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            repeat_count INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_stem_arrangement_items_position UNIQUE (arrangement_id, position)
        )
    """))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_stem_arrangement_items_arrangement_id "
        "ON stem_arrangement_items (arrangement_id)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_stem_arrangement_items_part_id "
        "ON stem_arrangement_items (part_id)"
    ))

    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS stem_arrangement_tracks (
            id UUID PRIMARY KEY,
            arrangement_id UUID NOT NULL REFERENCES stem_arrangements(id) ON DELETE CASCADE,
            instrument stem_instrument NOT NULL,
            file_s3_key VARCHAR(500),
            preview_s3_key VARCHAR(500),
            aes_key TEXT,
            aes_iv TEXT,
            duration INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'rendering',
            created_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_stem_arrangement_tracks_instrument UNIQUE (arrangement_id, instrument)
        )
    """))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_stem_arrangement_tracks_arrangement_id "
        "ON stem_arrangement_tracks (arrangement_id)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS stem_arrangement_tracks"))
    op.execute(sa.text("DROP TABLE IF EXISTS stem_arrangement_items"))
    op.execute(sa.text("DROP TABLE IF EXISTS stem_arrangements"))

    op.execute(sa.text("DROP INDEX IF EXISTS uq_stems_pack_instrument_long_form"))
    op.execute(sa.text("ALTER TABLE stems DROP CONSTRAINT IF EXISTS uq_stems_part_instrument"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_stems_part_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_stems_instrument"))
    op.execute(sa.text("ALTER TABLE stems DROP COLUMN IF EXISTS status"))
    op.execute(sa.text("ALTER TABLE stems DROP COLUMN IF EXISTS part_id"))
    op.execute(sa.text("ALTER TABLE stems DROP COLUMN IF EXISTS instrument"))

    op.execute(sa.text("DROP INDEX IF EXISTS ix_stem_parts_pack_position"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_stem_parts_stem_pack_id"))
    op.execute(sa.text("DROP TABLE IF EXISTS stem_parts"))

    op.execute(sa.text("DROP INDEX IF EXISTS ix_stem_packs_pack_type"))
    op.execute(sa.text("ALTER TABLE stem_packs DROP COLUMN IF EXISTS pack_type"))

    op.execute(sa.text("DROP TYPE IF EXISTS song_section"))
    op.execute(sa.text("DROP TYPE IF EXISTS stem_instrument"))
    op.execute(sa.text("DROP TYPE IF EXISTS stem_pack_type"))
