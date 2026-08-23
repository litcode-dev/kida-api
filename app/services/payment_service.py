import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.purchase import Purchase, PurchaseType, PaymentProvider
from app.models.loop import Loop
from app.models.stem_pack import StemPack
from app.models.user import User
from app.exceptions import NotFoundError, AppError
from app.schemas.purchase import CheckoutRequest
from app.services import payments

log = structlog.get_logger()

DEFAULT_CURRENCY = "NGN"


async def create_checkout_session(
    db: AsyncSession,
    user: User,
    request: CheckoutRequest,
) -> dict:
    if request.loop_id:
        product = await db.get(Loop, request.loop_id)
        if not product:
            raise NotFoundError("Loop not found")
        metadata = {"loop_id": str(request.loop_id), "user_id": str(user.id)}
    else:
        product = await db.get(StemPack, request.stem_pack_id)
        if not product:
            raise NotFoundError("StemPack not found")
        metadata = {"stem_pack_id": str(request.stem_pack_id), "user_id": str(user.id)}

    gateway = payments.get_gateway(request.provider)
    session = await gateway.create_checkout(
        amount=product.price,
        currency=DEFAULT_CURRENCY,
        reference=str(uuid.uuid4()),
        email=user.email,
        description=product.title,
        metadata={**metadata, "customer_name": user.full_name},
    )
    # The gateway's reference, not ours: Stripe answers verification by its own
    # session id, so whatever comes back here is what the webhook will carry.
    return {"checkout_url": session.checkout_url, "payment_reference": session.reference}


async def handle_webhook(
    db: AsyncSession,
    provider: PaymentProvider | str,
    payload: bytes,
    signature: str | None,
) -> None:
    """Record a one-time purchase from any provider's webhook.

    The body is only ever a notification. Once its signature checks out the
    transaction is re-read from the gateway, and the amount and metadata used
    below come from *that* answer — not from the request, which is why a
    forged or replayed body cannot name its own buyer or price.
    """
    gateway = payments.get_gateway(provider)
    if not gateway.verify_webhook(payload, signature):
        raise AppError("Invalid webhook signature", status_code=400)

    event = gateway.parse_webhook(payload)
    if not event.is_payment_success or not event.reference:
        return

    verified = await gateway.verify_transaction(event.reference)
    if not verified.succeeded:
        log.info(
            "webhook_transaction_not_successful",
            provider=gateway.provider.value, reference=event.reference,
        )
        return

    user_id = verified.metadata.get("user_id")
    loop_id = verified.metadata.get("loop_id")
    stem_pack_id = verified.metadata.get("stem_pack_id")
    if not user_id or not (loop_id or stem_pack_id):
        # A charge that did not start from a checkout of ours — nothing to
        # grant, and no reason to fail the delivery and be retried forever.
        log.info(
            "webhook_without_purchase_metadata",
            provider=gateway.provider.value, reference=verified.reference,
        )
        return

    try:
        purchase = Purchase(
            user_id=uuid.UUID(user_id),
            loop_id=uuid.UUID(loop_id) if loop_id else None,
            stem_pack_id=uuid.UUID(stem_pack_id) if stem_pack_id else None,
            payment_reference=verified.reference,
            payment_provider=gateway.provider,
            amount_paid=verified.amount,
            purchase_type=PurchaseType.one_time,
        )
    except (ValueError, AttributeError, TypeError):
        log.warning(
            "webhook_metadata_not_usable",
            provider=gateway.provider.value, reference=verified.reference,
        )
        return

    existing = await db.scalar(
        select(Purchase).where(Purchase.payment_reference == verified.reference)
    )
    if existing:
        return

    db.add(purchase)
    await db.commit()

    # The purchase is recorded; a broker outage must not undo it.
    try:
        from app.tasks.notification_tasks import send_purchase_confirmation
        send_purchase_confirmation.delay(str(user_id), str(purchase.id))
    except Exception as exc:  # noqa: BLE001 - the purchase is already committed
        log.error(
            "purchase_confirmation.enqueue_failed",
            purchase_id=str(purchase.id), error=str(exc),
        )
