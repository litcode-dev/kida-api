import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppError
from app.models.iap_subscription import (
    IapSubscription,
    IapPlatform,
    IapSubscriptionSource,
    IapSubscriptionStatus,
    PremiumPlan,
)

# Store product IDs for Kiɗa Premium (identical on both stores).
PLAN_PRODUCT_IDS = {
    PremiumPlan.monthly: "kida.premium.monthly",
    PremiumPlan.yearly: "kida.premium.yearly",
}
PREMIUM_PRODUCT_IDS = set(PLAN_PRODUCT_IDS.values())

# Store-side verification is disabled (see verify_and_upsert) — the real
# billing period would otherwise come from Apple/Google's verify response,
# so it's assumed from the product id's naming convention instead.
_YEARLY_PERIOD = timedelta(days=365)
_MONTHLY_PERIOD = timedelta(days=30)

PLAN_PERIODS = {
    PremiumPlan.monthly: _MONTHLY_PERIOD,
    PremiumPlan.yearly: _YEARLY_PERIOD,
}


def plan_for_product(product_id: str | None) -> PremiumPlan | None:
    """The billing period a product id represents, by naming convention."""
    if not product_id:
        return None
    return PremiumPlan.yearly if "year" in product_id else PremiumPlan.monthly


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


def admin_view(user_id: uuid.UUID, sub: IapSubscription | None) -> dict:
    """The full entitlement record an admin needs to see, including how it got there."""
    plan = plan_for_product(sub.product_id) if sub else None
    return {
        "user_id": str(user_id),
        "active": is_active(sub),
        "plan": plan.value if plan else None,
        "product_id": sub.product_id if sub else None,
        "status": sub.status.value if sub else None,
        "source": sub.source.value if sub else None,
        "platform": sub.platform.value if sub and sub.platform else None,
        "admin_locked": sub.admin_locked if sub else False,
        "admin_note": sub.admin_note if sub else None,
        "expires_at": sub.expires_at.isoformat() if sub and sub.expires_at else None,
        "updated_at": sub.updated_at.isoformat() if sub and sub.updated_at else None,
    }


async def get_subscriptions_for_users(
    db: AsyncSession, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, IapSubscription]:
    """Entitlements for a batch of users, keyed by user id.

    One query for the whole batch — listing screens use this instead of calling
    get_subscription per row. Users without an entitlement are simply absent from
    the mapping.
    """
    if not user_ids:
        return {}
    rows = await db.scalars(
        select(IapSubscription).where(IapSubscription.user_id.in_(user_ids))
    )
    return {sub.user_id: sub for sub in rows.all()}


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

    Refuses entitlements an admin has deactivated — because receipts are taken
    on trust, a revoked user could otherwise restore premium simply by having
    the app re-post its receipt.
    """
    if platform not in ("ios", "android"):
        raise AppError("platform must be 'ios' or 'android'", status_code=400)

    # One row per user — switching monthly ↔ yearly updates the same entitlement.
    sub = await get_subscription(db, user_id)
    if sub is not None and sub.admin_locked:
        raise AppError(
            "This subscription has been deactivated by an administrator", status_code=403
        )
    if sub is None:
        sub = IapSubscription(user_id=user_id)
        db.add(sub)

    sub.platform = IapPlatform(platform)
    sub.product_id = product_id
    sub.store_transaction_id = hashlib.sha256(receipt.encode()).hexdigest()
    sub.status = IapSubscriptionStatus.active
    sub.source = IapSubscriptionSource.store
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


async def admin_activate(
    db: AsyncSession,
    user_id: uuid.UUID,
    plan: PremiumPlan,
    expires_at: datetime | None = None,
    note: str | None = None,
) -> IapSubscription:
    """Grant (or extend) Kiɗa Premium for a user without a store purchase.

    Updates the user's single entitlement row in place, so an admin can also use
    this to switch an existing subscriber between monthly and yearly. ``expires_at``
    defaults to the plan's billing period from now; pass one to set a custom end
    date. Activation clears any earlier admin deactivation.

    A store row's ``store_transaction_id`` and receipt are left untouched so the
    purchase stays traceable — only the plan, expiry and status change.
    """
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise AppError("expires_at must be in the future", status_code=400)
    else:
        expires_at = datetime.now(timezone.utc) + PLAN_PERIODS[plan]

    sub = await get_subscription(db, user_id)
    if sub is None:
        sub = IapSubscription(
            user_id=user_id,
            # No store transaction backs an admin grant; a synthetic id keeps the
            # uniqueness constraint satisfied without colliding with real receipts.
            store_transaction_id=f"admin:{uuid.uuid4()}",
        )
        db.add(sub)

    sub.product_id = PLAN_PRODUCT_IDS[plan]
    sub.status = IapSubscriptionStatus.active
    sub.source = IapSubscriptionSource.admin
    sub.expires_at = expires_at
    sub.admin_locked = False
    sub.admin_note = note

    await db.commit()
    await db.refresh(sub)
    return sub


async def admin_deactivate(
    db: AsyncSession,
    user_id: uuid.UUID,
    note: str | None = None,
) -> IapSubscription | None:
    """Revoke a user's Kiɗa Premium entitlement.

    Returns None when the user has no entitlement row at all. The row is kept and
    marked ``admin_locked`` rather than deleted, both for the audit trail and so a
    client re-posting its receipt cannot silently restore premium.

    Note this only ends server-side entitlement — it does not cancel the user's
    billing with Apple or Google, which they have to do from their store account.
    """
    sub = await get_subscription(db, user_id)
    if sub is None:
        return None

    sub.status = IapSubscriptionStatus.revoked
    sub.admin_locked = True
    sub.admin_note = note

    await db.commit()
    await db.refresh(sub)
    return sub
