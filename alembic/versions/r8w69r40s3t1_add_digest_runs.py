"""add digest_runs

One row per attempt at the daily new-content digest. The row is also the lock:
run_key is unique, so celery beat and the API's in-process scheduler can both
be pointed at the digest without anyone receiving it twice.

Revision ID: r8w69r40s3t1
Revises: q7v58q39r2s0
Create Date: 2026-08-19 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "r8w69r40s3t1"
down_revision = "q7v58q39r2s0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS digest_runs (
            id UUID PRIMARY KEY,
            run_key VARCHAR(40) NOT NULL UNIQUE,
            scheduled_for TIMESTAMPTZ NOT NULL,
            trigger VARCHAR(16) NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'running',
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            items INTEGER NOT NULL DEFAULT 0,
            recipients INTEGER NOT NULL DEFAULT 0,
            sent INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            claimed_ids JSONB,
            detail TEXT
        )
    """))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_digest_runs_started_at ON digest_runs (started_at)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_digest_runs_started_at"))
    op.execute(sa.text("DROP TABLE IF EXISTS digest_runs"))
