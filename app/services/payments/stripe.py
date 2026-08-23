"""Stripe Checkout, over the REST API directly rather than the SDK, so this
adapter looks like the others and adds no dependency.

Two things differ from the African gateways: amounts are in the currency's
minor unit (cents), and the signature header is a comma-separated list of a
timestamp and one or more v1 signatures over "{timestamp}.{body}".
"""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import ClassVar

import structlog

from app.config import get_settings
from app.exceptions import PaymentError
from app.models.purchase import PaymentProvider
from app.services.payments.base import (
    CheckoutSession, PaymentGateway, VerifiedTransaction, WebhookEvent, to_decimal,
)

log = structlog.get_logger()

STRIPE_BASE = "https://api.stripe.com/v1"
SUBUNITS = Decimal(100)
# Reject a replayed signature older than this, as Stripe's own libraries do.
SIGNATURE_TOLERANCE_SECONDS = 300


class StripeGateway(PaymentGateway):
    provider: ClassVar[PaymentProvider] = PaymentProvider.stripe
    signature_header: ClassVar[str] = "stripe-signature"
    label: ClassVar[str] = "Stripe"

    def __init__(self) -> None:
        settings = get_settings()
        self._secret_key = settings.stripe_secret_key
        self._webhook_secret = settings.stripe_webhook_secret
        self._success_url = f"{settings.frontend_url}/payments/callback?status=success"
        self._cancel_url = f"{settings.frontend_url}/payments/callback?status=cancelled"

    @property
    def is_configured(self) -> bool:
        return bool(self._secret_key)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    async def create_checkout(
        self, *, amount: Decimal, currency: str, reference: str, email: str,
        description: str, metadata: dict,
    ) -> CheckoutSession:
        self.require_configured()
        # Stripe takes form encoding with bracketed keys, and every metadata
        # value has to be a string.
        form = {
            "mode": "payment",
            "success_url": self._success_url,
            "cancel_url": self._cancel_url,
            "customer_email": email,
            "client_reference_id": reference,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(int(amount * SUBUNITS)),
            "line_items[0][price_data][product_data][name]": description,
        }
        for key, value in {**metadata, "reference": reference}.items():
            form[f"metadata[{key}]"] = str(value)

        data = await self._request(
            "POST", f"{STRIPE_BASE}/checkout/sessions",
            headers=self._headers(), data=form,
        )
        url = data.get("url")
        if not url:
            raise PaymentError("Stripe did not return a checkout URL")
        # The session id is what verification needs later, so it becomes the
        # reference we store — client_reference_id keeps ours attached too.
        return CheckoutSession(checkout_url=url, reference=data.get("id") or reference)

    async def verify_transaction(self, reference: str) -> VerifiedTransaction:
        """``reference`` is the Checkout Session id returned at create time."""
        self.require_configured()
        data = await self._request(
            "GET", f"{STRIPE_BASE}/checkout/sessions/{reference}", headers=self._headers(),
        )
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        return VerifiedTransaction(
            reference=data.get("client_reference_id") or data.get("id") or reference,
            succeeded=data.get("payment_status") == "paid",
            amount=to_decimal(data.get("amount_total")) / SUBUNITS,
            currency=(data.get("currency") or "usd").upper(),
            metadata=metadata,
            provider_transaction_id=data.get("payment_intent") or data.get("id"),
        )

    def verify_webhook(self, payload: bytes, signature: str | None) -> bool:
        if not self._webhook_secret:
            log.warning("stripe_webhook_secret_not_configured")
            return False
        if not signature:
            return False

        timestamp, candidates = _split_signature_header(signature)
        if timestamp is None or not candidates:
            return False
        if abs(time.time() - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
            log.warning("stripe_webhook_timestamp_outside_tolerance")
            return False

        signed = f"{int(timestamp)}.".encode() + payload
        expected = hmac.new(self._webhook_secret.encode(), signed, hashlib.sha256).hexdigest()
        # Stripe may send several v1 signatures during a secret rotation.
        return any(hmac.compare_digest(expected, candidate) for candidate in candidates)

    def parse_webhook(self, payload: bytes) -> WebhookEvent:
        event = self._loads(payload)
        obj = ((event.get("data") or {}).get("object")
               if isinstance(event.get("data"), dict) else {}) or {}
        kind = event.get("type")
        return WebhookEvent(
            is_payment_success=(
                kind == "checkout.session.completed" and obj.get("payment_status") == "paid"
            ),
            # Verification is by session id, so that is what the caller needs.
            reference=obj.get("id"),
            provider_transaction_id=obj.get("payment_intent"),
            raw_event=kind,
        )


def _split_signature_header(header: str) -> tuple[float | None, list[str]]:
    """Parse "t=1234,v1=abc,v1=def" into its timestamp and v1 signatures."""
    timestamp, signatures = None, []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = float(value)
            except ValueError:
                return None, []
        elif key == "v1" and value:
            signatures.append(value)
    return timestamp, signatures
