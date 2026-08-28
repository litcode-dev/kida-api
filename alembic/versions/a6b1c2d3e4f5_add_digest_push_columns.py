"""record what became of the digest's push notification

The daily digest now goes out as a push to every device as well as an email.
Two nullable columns on digest_runs keep the outcome, so "the mail arrived but
no push did" is answerable from the run history rather than only from logs.

NULL means no push was attempted, which is what every run recorded before this
migration looks like — and what a run that sends no mail still looks like.

Revision ID: a6b1c2d3e4f5
Revises: f5b9c4d17e26
Create Date: 2026-08-28 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "a6b1c2d3e4f5"
down_revision = "f5b9c4d17e26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE digest_runs ADD COLUMN IF NOT EXISTS push_status VARCHAR(24)"
    ))
    op.execute(sa.text(
        "ALTER TABLE digest_runs ADD COLUMN IF NOT EXISTS push_detail TEXT"
    ))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE digest_runs DROP COLUMN IF EXISTS push_detail"))
    op.execute(sa.text("ALTER TABLE digest_runs DROP COLUMN IF EXISTS push_status"))
