"""The payment gateway factory.

Ask for a provider by name and get something that speaks the interface in
``base``. Callers never import an adapter directly, so adding a fifth gateway
is a new module and one line in the registry below — no branching in the
services, the routers, or anywhere else::

    gateway = get_gateway(request.provider)
    session = await gateway.create_checkout(amount=..., reference=..., ...)
"""
from __future__ import annotations

from app.exceptions import AppError
from app.models.purchase import PaymentProvider
from app.services.payments.base import (
    CheckoutSession, GATEWAY_TIMEOUT, PaymentGateway, VerifiedTransaction, WebhookEvent,
)
from app.services.payments.flutterwave import FlutterwaveGateway
from app.services.payments.paystack import PaystackGateway
from app.services.payments.squad import SquadGateway
from app.services.payments.stripe import StripeGateway

_REGISTRY: dict[PaymentProvider, type[PaymentGateway]] = {
    PaymentProvider.flutterwave: FlutterwaveGateway,
    PaymentProvider.paystack: PaystackGateway,
    PaymentProvider.squad: SquadGateway,
    PaymentProvider.stripe: StripeGateway,
}

__all__ = [
    "CheckoutSession", "GATEWAY_TIMEOUT", "PaymentGateway", "VerifiedTransaction",
    "WebhookEvent", "coerce_provider", "configured_providers", "get_gateway",
    "supported_providers",
]


def coerce_provider(provider: PaymentProvider | str) -> PaymentProvider:
    """Accept the enum or its wire name; refuse anything else with a 400."""
    if isinstance(provider, PaymentProvider):
        return provider
    try:
        return PaymentProvider(str(provider).lower())
    except ValueError:
        raise AppError(
            f"Unknown payment provider '{provider}'. Supported: "
            + ", ".join(p.value for p in supported_providers()),
            status_code=400,
        )


def get_gateway(provider: PaymentProvider | str) -> PaymentGateway:
    """The gateway for this provider.

    Built per call rather than cached, so a settings change (or a test's
    monkeypatched environment) is picked up without a stale key surviving in a
    module-level instance.
    """
    return _REGISTRY[coerce_provider(provider)]()


def supported_providers() -> list[PaymentProvider]:
    """Every provider with an adapter, configured or not."""
    return list(_REGISTRY)


def configured_providers() -> list[PaymentProvider]:
    """The providers this deployment actually has credentials for."""
    return [p for p in _REGISTRY if get_gateway(p).is_configured]
