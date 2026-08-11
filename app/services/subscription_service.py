import uuid
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.subscription import Subscription, SubscriptionPlan, SubscriptionStatus
from app.models.purchase import PaymentProvider
from app.models.user import User
from app.config import get_settings
from app.exceptions import AppError
from app.services import flutterwave_service, paystack_service


async def get_active_subscription(db: AsyncSession, user_id: uuid.UUID) -> Subscription | None:
    now = datetime.now(timezone.utc)
    return await db.scalar(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.active,
            Subscription.expires_at > now,
        )
    )


async def create_subscription(
    db: AsyncSession,
    user_id: uuid.UUID,
    provider: PaymentProvider,
    payment_reference: str,
    amount: Decimal,
) -> Subscription:
    now = datetime.now(timezone.utc)
    sub = Subscription(
        user_id=user_id,
        plan=SubscriptionPlan.premium,
        status=SubscriptionStatus.active,
        provider=provider,
        payment_reference=payment_reference,
        amount_paid=amount,
        ai_quota=10,
        ai_quota_used=0,
        billing_period_start=now,
        expires_at=now + timedelta(days=30),
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


async def renew_subscription(
    db: AsyncSession,
    user_id: uuid.UUID,
    provider: PaymentProvider,
    payment_reference: str,
    amount: Decimal,
) -> Subscription:
    """Expire the current active subscription and create a fresh one for the new billing period."""
    existing = await get_active_subscription(db, user_id)
    if existing:
        existing.status = SubscriptionStatus.expired
        await db.commit()
    return await create_subscription(db, user_id, provider, payment_reference, amount)


async def expire_active_subscription(db: AsyncSession, user_id: uuid.UUID) -> Subscription | None:
    """Mark a user's active legacy web subscription expired, returning it if there was one.

    Used when an admin revokes premium, so the AI quota granted by a web payment
    goes away with the store entitlement instead of outliving it.
    """
    sub = await get_active_subscription(db, user_id)
    if sub is None:
        return None
    sub.status = SubscriptionStatus.expired
    await db.commit()
    await db.refresh(sub)
    return sub


async def _process_subscription_webhook(
    db: AsyncSession,
    user_id: str,
    payment_reference: str,
    amount: Decimal,
    provider: PaymentProvider,
) -> None:
    uid = uuid.UUID(user_id)
    existing = await get_active_subscription(db, uid)
    if existing:
        await renew_subscription(db, uid, provider, payment_reference, amount)
    else:
        await create_subscription(db, uid, provider, payment_reference, amount)


async def _process_extras_webhook(
    db: AsyncSession, user_id: str, quantity: int
) -> None:
    user = await db.get(User, uuid.UUID(user_id))
    if not user:
        return
    user.ai_extra_credits += quantity
    await db.commit()


async def _process_download_extras_webhook(
    db: AsyncSession, user_id: str, quantity: int
) -> None:
    """Credit downloads bought once a monthly allowance ran out."""
    user = await db.get(User, uuid.UUID(user_id))
    if not user:
        return
    user.download_extra_credits += quantity
    await db.commit()


async def handle_flutterwave_webhook(
    db: AsyncSession, payload: bytes, verif_hash: str
) -> None:
    if not flutterwave_service.verify_webhook_signature(verif_hash):
        raise AppError("Invalid webhook signature", status_code=400)

    event = json.loads(payload)
    if event.get("event") != "charge.completed":
        return
    data = event.get("data", {})
    if data.get("status") != "successful":
        return

    tx_ref = data.get("tx_ref")
    transaction_id = str(data.get("id"))
    amount = Decimal(str(data.get("amount", 0)))

    verification = await flutterwave_service.verify_transaction(transaction_id)
    if verification.get("data", {}).get("status") != "successful":
        return

    # Deduplicate by payment_reference
    existing = await db.scalar(
        select(Subscription).where(Subscription.payment_reference == tx_ref)
    )
    if existing:
        return

    meta = data.get("meta", {})
    user_id = meta.get("user_id")
    payment_type = meta.get("type")

    if payment_type == "subscription":
        await _process_subscription_webhook(
            db, user_id, tx_ref, amount, PaymentProvider.flutterwave
        )
    elif payment_type == "ai_extras":
        quantity = meta.get("quantity", get_settings().ai_extra_credits_quantity)
        await _process_extras_webhook(db, user_id, int(quantity))
    elif payment_type == "download_extras":
        quantity = meta.get("quantity", get_settings().download_extra_credits_quantity)
        await _process_download_extras_webhook(db, user_id, int(quantity))


async def handle_paystack_webhook(
    db: AsyncSession, payload: bytes, x_paystack_signature: str
) -> None:
    if not paystack_service.verify_webhook_signature(payload, x_paystack_signature):
        raise AppError("Invalid webhook signature", status_code=400)

    event = json.loads(payload)
    if event.get("event") != "charge.success":
        return
    data = event.get("data", {})
    reference = data.get("reference")
    amount = Decimal(str(data.get("amount", 0))) / 100

    verification = await paystack_service.verify_transaction(reference)
    if not verification.get("status") or verification.get("data", {}).get("status") != "success":
        return

    existing = await db.scalar(
        select(Subscription).where(Subscription.payment_reference == reference)
    )
    if existing:
        return

    meta = data.get("metadata", {})
    user_id = meta.get("user_id")
    payment_type = meta.get("type")

    if payment_type == "subscription":
        await _process_subscription_webhook(
            db, user_id, reference, amount, PaymentProvider.paystack
        )
    elif payment_type == "ai_extras":
        quantity = meta.get("quantity", get_settings().ai_extra_credits_quantity)
        await _process_extras_webhook(db, user_id, int(quantity))
    elif payment_type == "download_extras":
        quantity = meta.get("quantity", get_settings().download_extra_credits_quantity)
        await _process_download_extras_webhook(db, user_id, int(quantity))
