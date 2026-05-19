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
    # Add store_product_id to content tables
    op.execute(sa.text("""
        ALTER TABLE loops
            ADD COLUMN IF NOT EXISTS store_product_id VARCHAR(255);
        ALTER TABLE drum_kits
            ADD COLUMN IF NOT EXISTS store_product_id VARCHAR(255);
        ALTER TABLE drone_pads
            ADD COLUMN IF NOT EXISTS store_product_id VARCHAR(255);
    """))

    # Extend purchases table for IAP
    op.execute(sa.text("""
        ALTER TABLE purchases
            ADD COLUMN IF NOT EXISTS item_id UUID,
            ADD COLUMN IF NOT EXISTS platform VARCHAR(10)
                CHECK (platform IN ('ios', 'android')),
            ADD COLUMN IF NOT EXISTS receipt TEXT,
            ADD COLUMN IF NOT EXISTS iap_transaction_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;

        -- Make payment_reference optional (IAP rows won't have one)
        ALTER TABLE purchases
            ALTER COLUMN payment_reference DROP NOT NULL;

        -- Make payment_provider optional (IAP rows won't have one)
        ALTER TABLE purchases
            ALTER COLUMN amount_paid DROP NOT NULL;

        -- Unique constraint: one IAP purchase per user+item
        DO $$ BEGIN
            ALTER TABLE purchases
                ADD CONSTRAINT uq_purchases_user_item UNIQUE (user_id, item_id);
        EXCEPTION WHEN duplicate_table THEN null;
        END $$;

        CREATE INDEX IF NOT EXISTS ix_purchases_item_id ON purchases (item_id);
        CREATE INDEX IF NOT EXISTS ix_purchases_iap_transaction_id
            ON purchases (iap_transaction_id)
            WHERE iap_transaction_id IS NOT NULL;
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE loops DROP COLUMN IF EXISTS store_product_id;
        ALTER TABLE drum_kits DROP COLUMN IF EXISTS store_product_id;
        ALTER TABLE drone_pads DROP COLUMN IF EXISTS store_product_id;

        DROP INDEX IF EXISTS ix_purchases_iap_transaction_id;
        DROP INDEX IF EXISTS ix_purchases_item_id;

        ALTER TABLE purchases
            DROP CONSTRAINT IF EXISTS uq_purchases_user_item,
            DROP COLUMN IF EXISTS item_id,
            DROP COLUMN IF EXISTS platform,
            DROP COLUMN IF EXISTS receipt,
            DROP COLUMN IF EXISTS iap_transaction_id,
            DROP COLUMN IF EXISTS verified_at;
    """))
