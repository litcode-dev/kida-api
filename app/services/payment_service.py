import json
import uuid
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.purchase import Purchase, PurchaseType, PaymentProvider
from app.models.loop import Loop
from app.models.stem_pack import StemPack
from app.models.user import User
from app.exceptions import NotFoundError, AppError
from app.schemas.purchase import CheckoutRequest
from app.services import flutterwave_service, paystack_service


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

    ref = str(uuid.uuid4())

    if request.provider == "flutterwave":
        result = await flutterwave_service.initialize_payment(
            amount=float(product.price),
            email=user.email,
            name=user.full_name,
            product_name=product.title,
            metadata=metadata,
            tx_ref=ref,
        )
        return {"checkout_url": result["payment_link"], "payment_reference": ref}

    # paystack
    amount_kobo = int(product.price * 100)
    result = await paystack_service.initialize_payment(
        amount_kobo=amount_kobo,
        email=user.email,
        reference=ref,
        metadata=metadata,
    )
    return {"checkout_url": result["authorization_url"], "payment_reference": ref}


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

    # Verify with Flutterwave API before recording the purchase
    verification = await flutterwave_service.verify_transaction(transaction_id)
    if verification.get("data", {}).get("status") != "successful":
        return

    meta = data.get("meta", {})
    user_id = meta.get("user_id")
    loop_id = meta.get("loop_id")
    stem_pack_id = meta.get("stem_pack_id")

    existing = await db.scalar(
        select(Purchase).where(Purchase.payment_reference == tx_ref)
    )
    if existing:
        return

    purchase = Purchase(
        user_id=uuid.UUID(user_id),
        loop_id=uuid.UUID(loop_id) if loop_id else None,
        stem_pack_id=uuid.UUID(stem_pack_id) if stem_pack_id else None,
        payment_reference=tx_ref,
        payment_provider=PaymentProvider.flutterwave,
        amount_paid=amount,
        purchase_type=PurchaseType.one_time,
    )
    db.add(purchase)
    await db.commit()

    from app.tasks.notification_tasks import send_purchase_confirmation
    send_purchase_confirmation.delay(str(user_id), str(purchase.id))


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
    amount_kobo = data.get("amount", 0)
    amount = Decimal(str(amount_kobo)) / 100

    # Verify with Paystack API before recording the purchase
    verification = await paystack_service.verify_transaction(reference)
    if not verification.get("status") or verification.get("data", {}).get("status") != "success":
        return

    meta = data.get("metadata", {})
    user_id = meta.get("user_id")
    loop_id = meta.get("loop_id")
    stem_pack_id = meta.get("stem_pack_id")

    existing = await db.scalar(
        select(Purchase).where(Purchase.payment_reference == reference)
    )
    if existing:
        return

    purchase = Purchase(
        user_id=uuid.UUID(user_id),
        loop_id=uuid.UUID(loop_id) if loop_id else None,
        stem_pack_id=uuid.UUID(stem_pack_id) if stem_pack_id else None,
        payment_reference=reference,
        payment_provider=PaymentProvider.paystack,
        amount_paid=amount,
        purchase_type=PurchaseType.one_time,
    )
    db.add(purchase)
    await db.commit()

    from app.tasks.notification_tasks import send_purchase_confirmation
    send_purchase_confirmation.delay(str(user_id), str(purchase.id))
