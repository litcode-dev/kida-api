"""Kiɗa Premium bought by card, for clients with no app store (the desktop app).

A payment buys a prepaid period — 30 days or a year — and nothing renews on
its own. The entitlement goes on the user's single ``IapSubscription`` row,
the one every premium check already reads (``iap_subscription_service.is_active``),
so a desktop payment unlocks exactly what an App Store or Play purchase does.

Flow:

1. :func:`start_checkout` opens a gateway checkout and returns its URL. The
   client opens it in the browser.
2. The gateway's webhook (``/subscriptions/webhook/{provider}``) and the
   client's own :func:`confirm` call both end in :func:`apply_verified`. Either
   can arrive first, or both; the ``premium_payments`` ledger, unique on the
   gateway reference, makes sure the period is added once.

Nothing here trusts the client or the webhook body about what was paid: the
user, plan and price are read back from the gateway's own record of the
transaction, where we put them at checkout.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import AppError
from app.models.iap_subscription import (
    IapSubscription,
    IapSubscriptionSource,
    IapSubscriptionStatus,
    PremiumPlan,
)
from app.models.premium_payment import PremiumPayment
from app.models.purchase import PaymentProvider
from app.models.user import User
from app.services import iap_subscription_service, payments
from app.services.iap_subscription_service import PLAN_PERIODS, PLAN_PRODUCT_IDS
from app.services.payments import VerifiedTransaction

log = structlog.get_logger()

CURRENCY = "USD"
PAYMENT_TYPE = "premium"
# Checkouts opened before Premium moved to dollars were in naira, in kobo, and
# carry no currency in their metadata. A payment on one of those is still
# honoured at the price it was opened at.
_LEGACY_CURRENCY = "NGN"

# Sources that renew through an app store. Selling a card period on top of one
# would charge the user twice, and the store's next event would overwrite it.
_STORE_SOURCES = {IapSubscriptionSource.store, IapSubscriptionSource.revenuecat}


def price_cents(plan: PremiumPlan) -> int:
    settings = get_settings()
    if plan is PremiumPlan.yearly:
        return settings.premium_yearly_price_usd_cents
    return settings.premium_monthly_price_usd_cents


def _major(minor: int) -> Decimal:
    """Cents (or kobo) to dollars (or naira): both currencies have 100 subunits."""
    return (Decimal(minor) / 100).quantize(Decimal("0.01"))


def plans() -> list[dict]:
    """What the client needs to draw the paywall."""
    return [
        {
            "plan": plan.value,
            "product_id": PLAN_PRODUCT_IDS[plan],
            "amount": str(_major(price_cents(plan))),
            "currency": CURRENCY,
            "period_days": PLAN_PERIODS[plan].days,
        }
        for plan in PremiumPlan
    ]


async def start_checkout(
    db: AsyncSession, user: User, plan: PremiumPlan, provider: PaymentProvider | str
) -> dict:
    sub = await iap_subscription_service.get_subscription(db, user.id)
    if sub is not None and sub.admin_locked:
        raise AppError(
            "This subscription has been deactivated by an administrator", status_code=403
        )
    if (
        sub is not None
        and sub.source in _STORE_SOURCES
        and iap_subscription_service.is_active(sub)
    ):
        raise AppError(
            "You already have Premium through the App Store or Google Play. "
            "It works on desktop too — sign in with the same account.",
            status_code=409,
        )

    cents = price_cents(plan)
    reference = str(uuid.uuid4())
    session = await payments.get_gateway(provider).create_checkout(
        amount=_major(cents),
        currency=CURRENCY,
        reference=reference,
        email=user.email,
        description=f"Kida Premium – {plan.value.capitalize()}",
        metadata={
            "user_id": str(user.id),
            "type": PAYMENT_TYPE,
            "plan": plan.value,
            # The price and currency at checkout, so a price change between
            # checkout and payment does not make an honest payment look short.
            "currency": CURRENCY,
            "amount_minor": cents,
            "customer_name": user.full_name,
        },
    )
    return {
        "checkout_url": session.checkout_url,
        "payment_reference": session.reference,
        "plan": plan.value,
        "amount": str(_major(cents)),
        "currency": CURRENCY,
    }


async def confirm(
    db: AsyncSession, user: User, provider: PaymentProvider | str, reference: str
) -> tuple[bool, IapSubscription | None]:
    """Check one of this user's premium payments with the gateway and apply it.

    Returns whether the payment has gone through, and the entitlement as it
    now stands. Safe to call repeatedly while the user is still paying.
    """
    gateway = payments.get_gateway(provider)
    verified = await gateway.verify_transaction(reference)
    meta = verified.metadata or {}
    if meta.get("type") != PAYMENT_TYPE or meta.get("user_id") != str(user.id):
        # Someone else's payment, or not a premium one: answer as if it did
        # not exist rather than confirm that it does.
        raise AppError("Payment not found", status_code=404)

    if not verified.succeeded:
        return False, await iap_subscription_service.get_subscription(db, user.id)

    sub = await apply_verified(db, gateway.provider, verified)
    return sub is not None, sub


async def apply_verified(
    db: AsyncSession, provider: PaymentProvider, verified: VerifiedTransaction
) -> IapSubscription | None:
    """Grant the period a verified, successful premium payment bought.

    Idempotent on the gateway reference. Returns the resulting entitlement, or
    None when the payment could not be applied (unknown user or plan, short
    payment) — each of those is logged for support to follow up.
    """
    meta = verified.metadata or {}
    try:
        user_id = uuid.UUID(str(meta.get("user_id")))
        plan = PremiumPlan(meta.get("plan"))
    except ValueError:
        log.error(
            "premium_payment.unreadable_metadata",
            provider=provider.value, reference=verified.reference,
        )
        return None

    already = await db.scalar(
        select(PremiumPayment).where(PremiumPayment.reference == verified.reference)
    )
    if already is not None:
        return await iap_subscription_service.get_subscription(db, user_id)

    currency, expected = _expected_charge(meta, plan)
    paid_currency = (verified.currency or "").upper()
    if paid_currency != currency or verified.amount < expected:
        log.error(
            "premium_payment.amount_mismatch",
            provider=provider.value, reference=verified.reference,
            amount=str(verified.amount), currency=verified.currency,
            expected=str(expected), expected_currency=currency,
        )
        return None

    if await db.get(User, user_id) is None:
        log.error(
            "premium_payment.unknown_user",
            provider=provider.value, reference=verified.reference,
        )
        return None

    sub = await iap_subscription_service.get_subscription(db, user_id)
    if sub is not None and sub.admin_locked:
        # Money was taken, but an admin has blocked this account's premium.
        # Not applied: support decides whether to refund or unlock.
        log.error(
            "premium_payment.paid_while_admin_locked",
            user_id=str(user_id), provider=provider.value, reference=verified.reference,
        )
        return sub

    now = datetime.now(timezone.utc)
    # Paying early adds to what is left rather than restarting the clock.
    start = sub.expires_at if iap_subscription_service.is_active(sub) else now
    end = start + PLAN_PERIODS[plan]

    if sub is None:
        sub = IapSubscription(user_id=user_id)
        db.add(sub)
    elif sub.source in _STORE_SOURCES and iap_subscription_service.is_active(sub):
        # Checkout refuses this, so the store subscription started after the
        # checkout did. The time is still added; the store's next event will
        # replace it, which support may need to refund.
        log.warning(
            "premium_payment.applied_over_store_subscription",
            user_id=str(user_id), reference=verified.reference,
        )

    sub.platform = None
    sub.product_id = PLAN_PRODUCT_IDS[plan]
    sub.store_transaction_id = f"{provider.value}:{verified.reference}"
    sub.app_user_id = None
    sub.status = IapSubscriptionStatus.active
    sub.source = IapSubscriptionSource.web
    sub.expires_at = end
    sub.latest_receipt = None
    sub.raw_payload = None

    db.add(PremiumPayment(
        user_id=user_id,
        provider=provider,
        reference=verified.reference,
        provider_transaction_id=verified.provider_transaction_id,
        plan=plan.value,
        amount=verified.amount,
        currency=paid_currency,
        period_start=start,
        period_end=end,
    ))
    try:
        await db.commit()
    except IntegrityError:
        # The webhook and the client's confirm landed together and the other
        # one recorded this reference first. Its result stands.
        await db.rollback()
        return await iap_subscription_service.get_subscription(db, user_id)

    await db.refresh(sub)
    log.info(
        "premium_payment.applied",
        user_id=str(user_id), plan=plan.value, provider=provider.value,
        expires_at=end.isoformat(),
    )
    return sub


def _expected_charge(meta: dict, plan: PremiumPlan) -> tuple[str, Decimal]:
    """The currency and minimum amount a payment must match, as set at checkout."""
    if "amount_kobo" in meta and "currency" not in meta:
        # Opened before the switch to dollars.
        kobo = _as_int(meta.get("amount_kobo"), 0)
        return _LEGACY_CURRENCY, _major(kobo) if kobo else Decimal("Infinity")
    currency = str(meta.get("currency") or CURRENCY).upper()
    return currency, _major(_as_int(meta.get("amount_minor"), price_cents(plan)))


def _as_int(value, default: int) -> int:
    """Metadata numbers can come back from a gateway as strings."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
