"""Flutterwave. Amounts in major units; our reference travels as tx_ref."""
from __future__ import annotations

import hmac
from decimal import Decimal
from typing import ClassVar

import structlog

from app.config import get_settings
from app.models.purchase import PaymentProvider
from app.services.payments.base import (
    CheckoutSession, PaymentGateway, VerifiedTransaction, WebhookEvent, to_decimal,
)

log = structlog.get_logger()


class FlutterwaveGateway(PaymentGateway):
    provider: ClassVar[PaymentProvider] = PaymentProvider.flutterwave
    signature_header: ClassVar[str] = "verif-hash"
    label: ClassVar[str] = "Flutterwave"

    def __init__(self) -> None:
        settings = get_settings()
        self._secret_key = settings.flutterwave_secret_key or settings.flw_secret_key
        # Two names for the same dashboard value, kept for older deployments.
        self._webhook_hash = settings.flutterwave_secret_hash or settings.flw_hash
        self._base_url = settings.flutterwave_base_url.rstrip("/")
        self._redirect_url = f"{settings.frontend_url}/payments/callback"

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
            "POST", f"{self._base_url}/payments",
            headers=self._headers(),
            json={
                "tx_ref": reference,
                "amount": str(amount),
                "currency": currency,
                "redirect_url": self._redirect_url,
                "customer": {"email": email, "name": metadata.get("customer_name", email)},
                "meta": metadata,
                "customizations": {"title": description},
            },
        )
        link = (data.get("data") or {}).get("link")
        if data.get("status") != "success" or not link:
            raise self._initialization_failed(data)
        return CheckoutSession(checkout_url=link, reference=reference)

    async def verify_transaction(self, reference: str) -> VerifiedTransaction:
        """Flutterwave verifies by *its* transaction id, not our tx_ref, so the
        caller passes whichever it holds — the id from a webhook when it has
        one, otherwise our reference through the by-reference endpoint."""
        self.require_configured()
        data = await self._request(
            "GET", f"{self._base_url}/transactions/verify_by_reference",
            headers=self._headers(), params={"tx_ref": reference},
        )
        return self._to_verified(data, fallback_reference=reference)

    async def verify_transaction_by_id(self, transaction_id: str) -> VerifiedTransaction:
        self.require_configured()
        data = await self._request(
            "GET", f"{self._base_url}/transactions/{transaction_id}/verify",
            headers=self._headers(),
        )
        return self._to_verified(data, fallback_reference="")

    def _to_verified(self, data: dict, fallback_reference: str) -> VerifiedTransaction:
        body = data.get("data") or {}
        return VerifiedTransaction(
            reference=body.get("tx_ref") or fallback_reference,
            succeeded=data.get("status") == "success" and body.get("status") == "successful",
            amount=to_decimal(body.get("amount")),
            currency=body.get("currency") or "NGN",
            metadata=body.get("meta") if isinstance(body.get("meta"), dict) else {},
            provider_transaction_id=str(body["id"]) if body.get("id") is not None else None,
        )

    def verify_webhook(self, payload: bytes, signature: str | None) -> bool:
        if not self._webhook_hash:
            # Fail closed. Comparing against an empty secret would authenticate
            # an empty header, which is how an unconfigured deployment ends up
            # accepting forged events.
            log.warning("flutterwave_webhook_secret_not_configured")
            return False
        if not signature:
            return False
        return hmac.compare_digest(signature, self._webhook_hash)

    def parse_webhook(self, payload: bytes) -> WebhookEvent:
        event = self._loads(payload)
        body = event.get("data") if isinstance(event.get("data"), dict) else {}
        transaction_id = body.get("id")
        return WebhookEvent(
            is_payment_success=(
                event.get("event") == "charge.completed"
                and body.get("status") == "successful"
            ),
            reference=body.get("tx_ref"),
            provider_transaction_id=str(transaction_id) if transaction_id is not None else None,
            raw_event=event.get("event"),
        )

    def _initialization_failed(self, data: dict):
        from app.exceptions import PaymentError
        return PaymentError(data.get("message") or "Flutterwave initialization failed")
