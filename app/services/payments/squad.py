"""Squad (GTCO). Kobo on the wire like Paystack, and a webhook signed with
HMAC-SHA512 of the raw body, sent uppercase in x-squad-encrypted-body.
"""
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

SUBUNITS = Decimal(100)


class SquadGateway(PaymentGateway):
    provider: ClassVar[PaymentProvider] = PaymentProvider.squad
    signature_header: ClassVar[str] = "x-squad-encrypted-body"
    label: ClassVar[str] = "Squad"

    def __init__(self) -> None:
        settings = get_settings()
        self._secret_key = settings.squad_secret_key
        self._base_url = settings.squad_base_url.rstrip("/")
        self._callback_url = f"{settings.frontend_url}/payments/callback"

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
            "POST", f"{self._base_url}/transaction/initiate",
            headers=self._headers(),
            json={
                "amount": int(amount * SUBUNITS),
                "email": email,
                "currency": currency,
                "initiate_type": "inline",
                "transaction_ref": reference,
                "callback_url": self._callback_url,
                "customer_name": metadata.get("customer_name", email),
                "metadata": {**metadata, "description": description},
            },
        )
        body = data.get("data") or {}
        url = body.get("checkout_url")
        if not url:
            raise PaymentError(data.get("message") or "Squad initialization failed")
        return CheckoutSession(
            checkout_url=url, reference=body.get("transaction_ref") or reference
        )

    async def verify_transaction(self, reference: str) -> VerifiedTransaction:
        self.require_configured()
        data = await self._request(
            "GET", f"{self._base_url}/transaction/verify/{reference}",
            headers=self._headers(),
        )
        body = data.get("data") or {}
        status = str(body.get("transaction_status") or "").lower()
        return VerifiedTransaction(
            reference=body.get("transaction_ref") or reference,
            succeeded=status == "success",
            amount=to_decimal(body.get("transaction_amount")) / SUBUNITS,
            currency=body.get("transaction_currency_id") or "NGN",
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
            provider_transaction_id=body.get("gateway_transaction_ref"),
        )

    def verify_webhook(self, payload: bytes, signature: str | None) -> bool:
        if not self._secret_key:
            log.warning("squad_webhook_secret_not_configured")
            return False
        if not signature:
            return False
        expected = hmac.new(
            self._secret_key.encode(), payload, hashlib.sha512
        ).hexdigest().upper()
        return hmac.compare_digest(expected, signature.upper())

    def parse_webhook(self, payload: bytes) -> WebhookEvent:
        event = self._loads(payload)
        # Squad capitalizes its webhook keys where its REST API does not.
        body = event.get("Body") if isinstance(event.get("Body"), dict) else event
        kind = str(event.get("Event") or event.get("event") or "")
        status = str(body.get("transaction_status") or "").lower()
        return WebhookEvent(
            is_payment_success=kind.lower() == "charge_successful" or status == "success",
            reference=body.get("transaction_ref") or body.get("merchant_reference"),
            provider_transaction_id=body.get("gateway_transaction_ref"),
            raw_event=kind or None,
        )
