"""add iap purchases

Revision ID: o4p36k15l2m8
Revises: n3o25j04k1l7
Create Date: 2026-05-19 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "o4p36k15l2m8"
down_revision = "n3o25j04k1l7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE loops ADD COLUMN IF NOT EXISTS store_product_id VARCHAR(255)"))
    op.execute(sa.text("ALTER TABLE drum_kits ADD COLUMN IF NOT EXISTS store_product_id VARCHAR(255)"))
    op.execute(sa.text("ALTER TABLE drone_pads ADD COLUMN IF NOT EXISTS store_product_id VARCHAR(255)"))

    op.execute(sa.text("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS item_id UUID"))
    op.execute(sa.text("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS platform VARCHAR(10) CHECK (platform IN ('ios', 'android'))"))
    op.execute(sa.text("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS receipt TEXT"))
    op.execute(sa.text("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS iap_transaction_id VARCHAR(255)"))
    op.execute(sa.text("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ"))
    op.execute(sa.text("ALTER TABLE purchases ALTER COLUMN payment_reference DROP NOT NULL"))
    op.execute(sa.text("ALTER TABLE purchases ALTER COLUMN amount_paid DROP NOT NULL"))

    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE purchases ADD CONSTRAINT uq_purchases_user_item UNIQUE (user_id, item_id);
        EXCEPTION WHEN duplicate_table THEN null;
        END $$
    """))

    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_purchases_item_id ON purchases (item_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_purchases_iap_transaction_id ON purchases (iap_transaction_id) WHERE iap_transaction_id IS NOT NULL"))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_purchases_iap_transaction_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_purchases_item_id"))
    op.execute(sa.text("ALTER TABLE purchases DROP CONSTRAINT IF EXISTS uq_purchases_user_item"))
    op.execute(sa.text("ALTER TABLE purchases DROP COLUMN IF EXISTS verified_at"))
    op.execute(sa.text("ALTER TABLE purchases DROP COLUMN IF EXISTS iap_transaction_id"))
    op.execute(sa.text("ALTER TABLE purchases DROP COLUMN IF EXISTS receipt"))
    op.execute(sa.text("ALTER TABLE purchases DROP COLUMN IF EXISTS platform"))
    op.execute(sa.text("ALTER TABLE purchases DROP COLUMN IF EXISTS item_id"))
    op.execute(sa.text("ALTER TABLE loops DROP COLUMN IF EXISTS store_product_id"))
    op.execute(sa.text("ALTER TABLE drum_kits DROP COLUMN IF EXISTS store_product_id"))
    op.execute(sa.text("ALTER TABLE drone_pads DROP COLUMN IF EXISTS store_product_id"))
