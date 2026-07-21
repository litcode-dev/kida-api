import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppError
from app.models.iap_subscription import (
    IapSubscription,
    IapPlatform,
    IapSubscriptionStatus,
)

# Store product IDs for Kiɗa Premium (identical on both stores).
PREMIUM_PRODUCT_IDS = {"kida.premium.monthly", "kida.premium.yearly"}

# Store-side verification is disabled (see verify_and_upsert) — the real
# billing period would otherwise come from Apple/Google's verify response,
# so it's assumed from the product id's naming convention instead.
_YEARLY_PERIOD = timedelta(days=365)
_MONTHLY_PERIOD = timedelta(days=30)


def _assumed_expiry(product_id: str) -> datetime:
    period = _YEARLY_PERIOD if "year" in product_id else _MONTHLY_PERIOD
    return datetime.now(timezone.utc) + period


# Statuses that still grant entitlement (grace = billing grace period).
ACTIVE_STATUSES = {IapSubscriptionStatus.active, IapSubscriptionStatus.grace}


async def get_subscription(db: AsyncSession, user_id: uuid.UUID) -> IapSubscription | None:
    return await db.scalar(
        select(IapSubscription).where(IapSubscription.user_id == user_id)
    )


def is_active(sub: IapSubscription | None) -> bool:
    """Entitled if the latest verified subscription is not expired, grace included."""
    if sub is None or sub.status not in ACTIVE_STATUSES:
        return False
    if sub.expires_at is None:
        return False
    return sub.expires_at > datetime.now(timezone.utc)


def entitlement(sub: IapSubscription | None) -> dict:
    active = is_active(sub)
    return {
        "active": active,
        "expires_at": sub.expires_at.isoformat() if sub and sub.expires_at else None,
        "product_id": sub.product_id if sub else None,
    }


async def verify_and_upsert(
    db: AsyncSession,
    user_id: uuid.UUID,
    product_id: str,
    platform: str,
    receipt: str,
) -> IapSubscription:
    """Records the user's subscription entitlement from the client's claim.

    Store-side verification with Apple/Google is disabled — the app trusts
    the platform/product_id/receipt the client reports instead of confirming
    them against the App Store / Play Developer API. The billing period is
    assumed from the product id (see _assumed_expiry) since the real expiry
    would otherwise come from the store's response.

    Idempotent: re-posting the same receipt updates the user's existing row
    (keyed on user_id, deduplicated on store_transaction_id) rather than
    inserting a duplicate.
    """
    if platform not in ("ios", "android"):
        raise AppError("platform must be 'ios' or 'android'", status_code=400)

    # One row per user — switching monthly ↔ yearly updates the same entitlement.
    sub = await get_subscription(db, user_id)
    if sub is None:
        sub = IapSubscription(user_id=user_id)
        db.add(sub)

    sub.platform = IapPlatform(platform)
    sub.product_id = product_id
    sub.store_transaction_id = hashlib.sha256(receipt.encode()).hexdigest()
    sub.status = IapSubscriptionStatus.active
    sub.expires_at = _assumed_expiry(product_id)
    sub.latest_receipt = receipt
    sub.raw_payload = None

    try:
        await db.commit()
    except IntegrityError:
        # store_transaction_id is unique — the receipt belongs to another account.
        await db.rollback()
        raise AppError("This purchase is already linked to another account", status_code=400)

    await db.refresh(sub)
    return sub
