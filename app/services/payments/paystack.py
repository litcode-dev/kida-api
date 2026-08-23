"""Paystack. Amounts on the wire are in kobo — minor units of NGN."""
from __future__ import annotations

import hashlib
import hmac
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

PAYSTACK_BASE = "https://api.paystack.co"
SUBUNITS = Decimal(100)


class PaystackGateway(PaymentGateway):
    provider: ClassVar[PaymentProvider] = PaymentProvider.paystack
    signature_header: ClassVar[str] = "x-paystack-signature"
    label: ClassVar[str] = "Paystack"

    def __init__(self) -> None:
        self._secret_key = get_settings().paystack_secret_key

    @property
    def is_configured(self) -> bool:
        return bool(self._secret_key)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._secret_key}"}

    async def create_checkout(
        self, *, amount: Decimal, currency: str, reference: str, email: str,
        description: str, metadata: dict,
    ) -> CheckoutSession:
        self.require_configured()
        data = await self._request(
            "POST", f"{PAYSTACK_BASE}/transaction/initialize",
            headers=self._headers(),
            json={
                "email": email,
                "amount": int(amount * SUBUNITS),
                "reference": reference,
                "currency": currency,
                "metadata": {**metadata, "description": description},
            },
        )
        url = (data.get("data") or {}).get("authorization_url")
        if not data.get("status") or not url:
            raise PaymentError(data.get("message") or "Paystack initialization failed")
        return CheckoutSession(checkout_url=url, reference=reference)

    async def verify_transaction(self, reference: str) -> VerifiedTransaction:
        self.require_configured()
        data = await self._request(
            "GET", f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers=self._headers(),
        )
        body = data.get("data") or {}
        return VerifiedTransaction(
            reference=body.get("reference") or reference,
            succeeded=bool(data.get("status")) and body.get("status") == "success",
            amount=to_decimal(body.get("amount")) / SUBUNITS,
            currency=body.get("currency") or "NGN",
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
            provider_transaction_id=str(body["id"]) if body.get("id") is not None else None,
        )

    def verify_webhook(self, payload: bytes, signature: str | None) -> bool:
        if not self._secret_key:
            log.warning("paystack_webhook_secret_not_configured")
            return False
        if not signature:
            # Header(None) means a request without the header arrives as None,
            # which compare_digest would raise a TypeError on.
            return False
        expected = hmac.new(self._secret_key.encode(), payload, hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook(self, payload: bytes) -> WebhookEvent:
        event = self._loads(payload)
        body = event.get("data") if isinstance(event.get("data"), dict) else {}
        transaction_id = body.get("id")
        return WebhookEvent(
            is_payment_success=event.get("event") == "charge.success",
            reference=body.get("reference"),
            provider_transaction_id=str(transaction_id) if transaction_id is not None else None,
            raw_event=event.get("event"),
        )
