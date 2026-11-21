"""add premium_payments and the 'web' entitlement source

Card payments (Paystack, Flutterwave, Squad, Stripe) can now buy a prepaid
period of Kiɗa Premium, for the desktop app where there is no app store.
The entitlement goes on the existing iap_subscriptions row with source 'web';
premium_payments is the ledger of each payment, unique on the gateway
reference so a payment is never applied twice.

Revision ID: i4d9e0f1a2b3
Revises: h3c8d9e0f1a2
Create Date: 2026-11-21 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "i4d9e0f1a2b3"
down_revision = "h3c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Transactional on PostgreSQL 12+; IF NOT EXISTS keeps re-runs safe.
    op.execute(sa.text("ALTER TYPE iapsubscriptionsource ADD VALUE IF NOT EXISTS 'web'"))

    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS premium_payments (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            provider paymentprovider NOT NULL,
            reference VARCHAR(255) NOT NULL,
            provider_transaction_id VARCHAR(255),
            plan VARCHAR(16) NOT NULL,
            amount NUMERIC(12, 2) NOT NULL,
            currency VARCHAR(3) NOT NULL,
            period_start TIMESTAMP WITH TIME ZONE NOT NULL,
            period_end TIMESTAMP WITH TIME ZONE NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
        )
    """))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_premium_payments_reference "
        "ON premium_payments (reference)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_premium_payments_user_id "
        "ON premium_payments (user_id)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS premium_payments"))
    # PostgreSQL cannot drop a single enum value; 'web' stays on
    # iapsubscriptionsource and is harmless once no rows use it.
