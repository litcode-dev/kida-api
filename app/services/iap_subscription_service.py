import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppError
from app.models.iap_subscription import (
    IapSubscription,
    IapPlatform,
    IapSubscriptionStatus,
)
from app.services.app_store_subscriptions import AppStoreSubscriptions
from app.services.play_subscriptions import PlaySubscriptions

# Store product IDs for Kiɗa Premium (identical on both stores).
PREMIUM_PRODUCT_IDS = {"kida.premium.monthly", "kida.premium.yearly"}

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
    *,
    play_client: PlaySubscriptions | None = None,
    app_store_client: AppStoreSubscriptions | None = None,
) -> IapSubscription:
    """Verify a store receipt and upsert the user's single entitlement row.

    Idempotent: re-posting the same receipt updates the user's existing row
    (keyed on user_id, deduplicated on store_transaction_id) rather than
    inserting a duplicate.
    """
    if platform == "android":
        client = play_client or PlaySubscriptions()
        verified = await client.verify(receipt)
    elif platform == "ios":
        client = app_store_client or AppStoreSubscriptions()
        verified = await client.verify(receipt)
    else:
        raise AppError("platform must be 'ios' or 'android'", status_code=400)

    resolved_product = verified.product_id or product_id
    status = IapSubscriptionStatus(verified.status)

    # One row per user — switching monthly ↔ yearly updates the same entitlement.
    sub = await get_subscription(db, user_id)
    if sub is None:
        sub = IapSubscription(user_id=user_id)
        db.add(sub)

    sub.platform = IapPlatform(platform)
    sub.product_id = resolved_product
    sub.store_transaction_id = verified.store_transaction_id
    sub.status = status
    sub.expires_at = verified.expires_at
    sub.latest_receipt = verified.latest_receipt
    sub.raw_payload = verified.raw_payload

    try:
        await db.commit()
    except IntegrityError:
        # store_transaction_id is unique — the receipt belongs to another account.
        await db.rollback()
        raise AppError("This purchase is already linked to another account", status_code=400)

    await db.refresh(sub)
    return sub
