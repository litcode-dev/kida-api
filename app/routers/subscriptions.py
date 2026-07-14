import uuid
from fastapi import APIRouter, Depends, Request, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.middleware.rate_limit import limiter
from app.services import subscription_service, flutterwave_service, paystack_service, iap_subscription_service
from app.schemas.subscription import (
    SubscriptionInitiateRequest, ExtraCreditsInitiateRequest, SubscriptionResponse,
    IapVerifyRequest, EntitlementResponse,
)
from app.schemas.common import success
from app.models.purchase import PaymentProvider
from app.exceptions import PaymentError
from app.config import get_settings

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.post("/initiate")
async def initiate_subscription(
    body: SubscriptionInitiateRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    settings = get_settings()
    ref = str(uuid.uuid4())
    metadata = {"user_id": str(user.id), "type": "subscription", "plan": "premium"}

    if body.provider == PaymentProvider.flutterwave:
        result = await flutterwave_service.initialize_payment(
            amount=settings.subscription_monthly_price / 100,
            email=user.email,
            name=user.full_name,
            product_name="LitMusic Premium – Monthly",
            metadata=metadata,
            tx_ref=ref,
        )
        return success({"checkout_url": result["payment_link"], "payment_reference": ref})

    result = await paystack_service.initialize_payment(
        amount_kobo=settings.subscription_monthly_price,
        email=user.email,
        reference=ref,
        metadata=metadata,
    )
    return success({"checkout_url": result["authorization_url"], "payment_reference": ref})


@router.post("/verify", response_model=EntitlementResponse)
@limiter.limit("20/minute")
async def verify_subscription(
    request: Request,
    body: IapVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Verify a store (App Store / Play) subscription receipt for the current user.

    Returns the resulting entitlement. Idempotent — re-posting the same receipt
    updates the user's single entitlement row instead of creating a duplicate.
    Returns 400 on an invalid/unverifiable receipt.
    """
    sub = await iap_subscription_service.verify_and_upsert(
        db, user.id, body.product_id, body.platform, body.receipt
    )
    return iap_subscription_service.entitlement(sub)


@router.get("/me", response_model=EntitlementResponse)
async def get_my_entitlement(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Current store-subscription entitlement (active includes the billing grace period)."""
    sub = await iap_subscription_service.get_subscription(db, user.id)
    return iap_subscription_service.entitlement(sub)


@router.get("/web/me")
async def get_my_web_subscription(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Legacy web-payment (Flutterwave/Paystack) premium subscription with AI quota."""
    sub = await subscription_service.get_active_subscription(db, user.id)
    if not sub:
        return success(None, "No active subscription")
    return success(SubscriptionResponse.model_validate(sub).model_dump())


@router.post("/extras/initiate")
async def initiate_extra_credits(
    body: ExtraCreditsInitiateRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    settings = get_settings()
    sub = await subscription_service.get_active_subscription(db, user.id)
    if not sub:
        raise PaymentError("Active premium subscription required to purchase extra credits")

    ref = str(uuid.uuid4())
    qty = settings.ai_extra_credits_quantity
    metadata = {"user_id": str(user.id), "type": "ai_extras", "quantity": qty}

    if body.provider == PaymentProvider.flutterwave:
        result = await flutterwave_service.initialize_payment(
            amount=settings.ai_extra_credits_price / 100,
            email=user.email,
            name=user.full_name,
            product_name=f"LitMusic AI Credits ×{qty}",
            metadata=metadata,
            tx_ref=ref,
        )
        return success({"checkout_url": result["payment_link"], "payment_reference": ref})

    result = await paystack_service.initialize_payment(
        amount_kobo=settings.ai_extra_credits_price,
        email=user.email,
        reference=ref,
        metadata=metadata,
    )
    return success({"checkout_url": result["authorization_url"], "payment_reference": ref})


@router.post("/webhook/flutterwave")
async def subscription_flutterwave_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    verif_hash: str = Header(None, alias="verif-hash"),
):
    payload = await request.body()
    await subscription_service.handle_flutterwave_webhook(db, payload, verif_hash)
    return {"received": True}


@router.post("/webhook/paystack")
async def subscription_paystack_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_paystack_signature: str = Header(None, alias="x-paystack-signature"),
):
    payload = await request.body()
    await subscription_service.handle_paystack_webhook(db, payload, x_paystack_signature)
    return {"received": True}
