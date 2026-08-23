from fastapi import APIRouter, Depends, Request, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.middleware.rate_limit import limiter
from app.services import payment_service, payments
from app.schemas.purchase import CheckoutRequest
from app.schemas.common import success

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/create-checkout")
@limiter.limit("10/minute")
async def create_checkout(
    request: Request,
    body: CheckoutRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    result = await payment_service.create_checkout_session(db, user, body)
    return success(result, "Checkout session created")


@router.post("/webhook/{provider}")
async def payment_webhook(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """One route for every gateway.

    The signature lives under a different header for each provider, so the
    gateway names its own; the route stays the same whichever one is calling.
    Existing dashboard URLs (/webhook/flutterwave, /webhook/paystack) keep
    working because they match this path.
    """
    gateway = payments.get_gateway(provider)
    signature = request.headers.get(gateway.signature_header)
    payload = await request.body()
    await payment_service.handle_webhook(db, gateway.provider, payload, signature)
    return {"received": True}


@router.get("/providers")
async def list_payment_providers():
    """Which gateways this deployment can actually take a payment through."""
    return success({
        "providers": [p.value for p in payments.configured_providers()],
        "supported": [p.value for p in payments.supported_providers()],
    })
