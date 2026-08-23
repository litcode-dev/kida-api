"""add squad and stripe to the paymentprovider enum

Revision ID: a04cd7bbb94d
Revises: r8w69r40s3t1
Create Date: 2026-08-23
"""
from alembic import op

revision = "a04cd7bbb94d"
down_revision = "r8w69r40s3t1"
branch_labels = None
depends_on = None

NEW_VALUES = ("squad", "stripe")
ENUM_NAME = "paymentprovider"
ORIGINAL_VALUES = ("flutterwave", "paystack")


def upgrade() -> None:
    # Postgres 12+ allows ADD VALUE inside a transaction; the value just cannot
    # be *used* until it commits, which no later step here does.
    for value in NEW_VALUES:
        op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Postgres cannot drop an enum value, so the type is rebuilt without them.

    Deliberately not defensive: if any purchase or subscription was paid
    through Squad or Stripe, the cast below fails and the downgrade stops
    rather than quietly rewriting how those payments were made.
    """
    values = ", ".join(f"'{v}'" for v in ORIGINAL_VALUES)
    op.execute(f"ALTER TYPE {ENUM_NAME} RENAME TO {ENUM_NAME}_old")
    op.execute(f"CREATE TYPE {ENUM_NAME} AS ENUM ({values})")
    # Both columns that use the type, under the two names they were given.
    for table, column in (("purchases", "payment_provider"), ("subscriptions", "provider")):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE {ENUM_NAME} USING {column}::text::{ENUM_NAME}"
        )
    op.execute(f"DROP TYPE {ENUM_NAME}_old")
