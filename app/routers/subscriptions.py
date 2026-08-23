import json
import uuid
from decimal import Decimal
from fastapi import APIRouter, Depends, Request, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.middleware.rate_limit import limiter
from app.services import (
    subscription_service, payments,
    iap_subscription_service, revenuecat_service,
)
from app.schemas.subscription import (
    SubscriptionInitiateRequest, ExtraCreditsInitiateRequest, SubscriptionResponse,
    IapVerifyRequest, EntitlementResponse,
)
from app.schemas.common import success
from app.models.purchase import PaymentProvider
from app.exceptions import AppError, PaymentError
from app.config import get_settings
import structlog

logger = structlog.get_logger()

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

    session = await payments.get_gateway(body.provider).create_checkout(
        amount=Decimal(settings.subscription_monthly_price) / 100,
        currency="NGN",
        reference=ref,
        email=user.email,
        description="LitMusic Premium – Monthly",
        metadata={**metadata, "customer_name": user.full_name},
    )
    return success({
        "checkout_url": session.checkout_url, "payment_reference": session.reference
    })


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


@router.post("/verify/revenuecat", response_model=EntitlementResponse)
@limiter.limit("20/minute")
async def verify_revenuecat_subscription(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Reconcile the current user's entitlement against RevenueCat.

    Pulls the subscriber's real entitlements from RevenueCat (using ``user.id`` as
    the ``app_user_id``) and stores them, so the returned expiry/status reflect
    what Apple/Google actually report — not an assumed billing period. Unlike the
    client-trusted ``/verify`` path, nothing here is taken from the client.

    Idempotent, and safe to call any time the app wants to refresh entitlement
    (e.g. on launch or after a purchase). Returns the resulting entitlement; an
    inactive one when the user has never subscribed.
    """
    client = revenuecat_service.RevenueCatClient()
    parsed = await client.fetch_entitlement(str(user.id))
    if parsed is None:
        # No RevenueCat premium entitlement; report whatever is already on file
        # (e.g. an admin grant) without overwriting it.
        sub = await iap_subscription_service.get_subscription(db, user.id)
        return iap_subscription_service.entitlement(sub)

    sub = await iap_subscription_service.apply_revenuecat_state(
        db,
        user.id,
        product_id=parsed.product_id,
        status=parsed.status,
        expires_at=parsed.expires_at,
        platform=parsed.platform,
        app_user_id=parsed.app_user_id,
        store_transaction_id=parsed.store_transaction_id,
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

    session = await payments.get_gateway(body.provider).create_checkout(
        amount=Decimal(settings.ai_extra_credits_price) / 100,
        currency="NGN",
        reference=ref,
        email=user.email,
        description=f"LitMusic AI Credits ×{qty}",
        metadata={**metadata, "customer_name": user.full_name},
    )
    return success({
        "checkout_url": session.checkout_url, "payment_reference": session.reference
    })


@router.post(
    "/downloads/extras/initiate",
    summary="Buy extra downloads",
    description=(
        "Starts a checkout for extra downloads, used once a monthly allowance is "
        "spent. Credits are shared across loops, drones and drum kits — one "
        "purchase covers whichever runs out — and do not expire at the end of the "
        "month.\n\n"
        "No subscription is required: anyone who has used up their allowance can "
        "buy more. The credits are added when the payment webhook confirms, not "
        "when this returns."
    ),
    responses={401: {"description": "Missing or invalid token"}},
)
@limiter.limit("10/minute")
async def initiate_extra_downloads(
    request: Request,
    body: ExtraCreditsInitiateRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    settings = get_settings()
    ref = str(uuid.uuid4())
    qty = settings.download_extra_credits_quantity
    metadata = {"user_id": str(user.id), "type": "download_extras", "quantity": qty}

    session = await payments.get_gateway(body.provider).create_checkout(
        # The configured price is in kobo; gateways take major units and
        # convert back to whatever they bill in.
        amount=Decimal(settings.download_extra_credits_price) / 100,
        currency="NGN",
        reference=ref,
        email=user.email,
        description=f"Kida Extra Downloads x{qty}",
        metadata={**metadata, "customer_name": user.full_name},
    )
    return success({
        "checkout_url": session.checkout_url,
        "payment_reference": session.reference,
        "quantity": qty,
    })





@router.post("/webhook/revenuecat")
async def subscription_revenuecat_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(None, alias="Authorization"),
):
    """Receive RevenueCat subscription lifecycle events.

    Authenticated with the shared Authorization header configured in the
    RevenueCat dashboard (fail-closed). Each event updates the subscriber's
    single ``IapSubscription`` row with RevenueCat's real status/expiry.

    Always returns 200 for an authenticated request whose body we could read —
    including events we intentionally skip (TEST pings, unmappable/anonymous
    app_user_ids) — so RevenueCat does not retry-storm on no-ops.
    """
    if not revenuecat_service.verify_webhook_auth(authorization):
        raise AppError("Invalid RevenueCat webhook authorization", status_code=401)

    raw = await request.body()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise AppError("Invalid RevenueCat webhook body", status_code=400)

    parsed = revenuecat_service.parse_webhook_event(payload)
    if parsed is None:
        return {"received": True, "applied": False}

    # app_user_id is our user UUID when the app called Purchases.logIn(user.id).
    # Anonymous ($RCAnonymousID:...) or otherwise non-UUID ids can't be mapped.
    try:
        user_id = uuid.UUID(parsed.app_user_id)
    except (ValueError, AttributeError, TypeError):
        logger.info("revenuecat_webhook_unmappable_user", app_user_id=parsed.app_user_id)
        return {"received": True, "applied": False}

    await iap_subscription_service.apply_revenuecat_state(
        db,
        user_id,
        product_id=parsed.product_id,
        status=parsed.status,
        expires_at=parsed.expires_at,
        platform=parsed.platform,
        app_user_id=parsed.app_user_id,
        store_transaction_id=parsed.store_transaction_id,
        raw_payload=payload,
    )
    return {"received": True, "applied": True}


@router.post("/webhook/{provider}")
async def subscription_payment_webhook(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Subscription and credit-pack payments, from any gateway.

    Declared last so /webhook/revenuecat above keeps its own handler: Starlette
    matches routes in order, and RevenueCat is a store aggregator rather than
    one of the card gateways behind the factory.
    """
    gateway = payments.get_gateway(provider)
    signature = request.headers.get(gateway.signature_header)
    payload = await request.body()
    await subscription_service.handle_webhook(db, gateway.provider, payload, signature)
    return {"received": True}
