"""Flutterwave, on the v4 API where v4 has an equivalent.

v4 changes three things this adapter depends on:

* **Auth.** Requests carry a short-lived OAuth 2.0 access token obtained with
  the client id and secret (client-credentials grant), not a static secret key.
  Tokens last ``expires_in`` seconds (600 today) and are cached per process and
  refreshed a minute early.
* **Verification.** A payment is a *charge* (``chg_...``) read from
  ``GET /charges/{id}``, or found by our reference with
  ``GET /charges?reference=...``. A successful charge has status ``succeeded``.
* **Webhooks.** The body is signed with HMAC-SHA256 under the dashboard's
  secret hash, base64-encoded, in the ``flutterwave-signature`` header. The
  event is ``type: charge.completed`` with ``data.status: succeeded``.

v4 has no hosted payment page: the only one Flutterwave offers is Checkout
(``POST /v3/payments``), which still needs the v3 secret key. Taking card
details through our own servers would put kida in PCI-DSS scope, so checkout
stays on that hosted page, and everything after it goes through v4 when the v4
credentials are set. Until then the v3 verify endpoints and the plain
``verif-hash`` webhook header keep working, so a deployment can move one
setting at a time.

Amounts are in major units on both APIs. Our reference travels as ``tx_ref``
into Checkout and comes back as ``reference`` on v4 charges.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import ClassVar

import httpx
import structlog

from app.config import get_settings
from app.exceptions import AppError
from app.models.purchase import PaymentProvider
from app.services.payments.base import (
    GATEWAY_TIMEOUT, CheckoutSession, PaymentGateway, VerifiedTransaction, WebhookEvent,
    to_decimal,
)

log = structlog.get_logger()

V4_SIGNATURE_HEADER = "flutterwave-signature"
V3_SIGNATURE_HEADER = "verif-hash"

# Refresh this long before the token says it expires, as Flutterwave advises,
# so a token cannot lapse between being read here and reaching their API.
TOKEN_REFRESH_MARGIN = 60

# (token_url, client_id) -> (access_token, monotonic expiry). Gateways are
# built per call, so the cache lives at module level; keying on the client id
# means rotating credentials never reuses the old account's token.
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


def clear_token_cache() -> None:
    """Forget cached v4 tokens (tests, credential rotation)."""
    _token_cache.clear()


class FlutterwaveGateway(PaymentGateway):
    provider: ClassVar[PaymentProvider] = PaymentProvider.flutterwave
    signature_header: ClassVar[str] = V4_SIGNATURE_HEADER
    label: ClassVar[str] = "Flutterwave"

    def __init__(self) -> None:
        settings = get_settings()
        # v3 secret key: only Checkout (the hosted page) still needs it.
        self._secret_key = settings.flutterwave_secret_key or settings.flw_secret_key
        # Two names for the same dashboard value, kept for older deployments.
        self._webhook_hash = settings.flutterwave_secret_hash or settings.flw_hash
        self._base_url = settings.flutterwave_base_url.rstrip("/")
        self._redirect_url = f"{settings.frontend_url}/payments/callback"
        # v4
        self._client_id = settings.flutterwave_client_id or settings.flw_client_id
        self._client_secret = settings.flutterwave_client_secret or settings.flw_client_secret
        self._v4_base_url = settings.flutterwave_v4_base_url.rstrip("/")
        self._token_url = settings.flutterwave_token_url

    # -- configuration -------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """Whether a payment can be started: the hosted page needs the secret key."""
        return bool(self._secret_key)

    @property
    def uses_v4(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _require_verifiable(self) -> None:
        if not (self.uses_v4 or self._secret_key):
            raise AppError(f"{self.label} is not configured", status_code=503)

    # -- v4 auth ---------------------------------------------------------------

    async def _access_token(self) -> str:
        key = (self._token_url, self._client_id)
        cached = _token_cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        async with httpx.AsyncClient(timeout=GATEWAY_TIMEOUT) as client:
            resp = await client.post(
                self._token_url,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        token = body.get("access_token") if isinstance(body, dict) else None
        if resp.status_code != 200 or not token:
            # A bad client id/secret is our misconfiguration, not the payer's
            # fault, so it surfaces as "unavailable" rather than a 400.
            log.warning(
                "flutterwave_token_failed",
                status=resp.status_code,
                error=(body.get("error") if isinstance(body, dict) else None),
            )
            raise AppError(
                f"{self.label} could not process this right now. Please try again.",
                status_code=502,
            )
        try:
            lifetime = int(body.get("expires_in") or 0)
        except (TypeError, ValueError):
            lifetime = 0
        # A token with no stated lifetime is used for this call only.
        if lifetime > TOKEN_REFRESH_MARGIN:
            _token_cache[key] = (token, time.monotonic() + lifetime - TOKEN_REFRESH_MARGIN)
        return token

    async def _v4_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {await self._access_token()}",
            "Content-Type": "application/json",
            # Alphanumeric, 12-255 chars; quoted to their support when debugging.
            "X-Trace-Id": uuid.uuid4().hex,
        }

    def _v3_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._secret_key}"}

    # -- payments --------------------------------------------------------------

    async def create_checkout(
        self, *, amount: Decimal, currency: str, reference: str, email: str,
        description: str, metadata: dict,
    ) -> CheckoutSession:
        """Flutterwave's hosted Checkout page. v4 has no hosted equivalent."""
        self.require_configured()
        data = await self._request(
            "POST", f"{self._base_url}/payments",
            headers=self._v3_headers(),
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
        """What happened to the payment carrying our reference.

        v4 first. If v4 has no charge under that reference (and a v3 key is
        set), ask v3, which is where a payment begun on the hosted page is on
        record for an account that has not finished moving to v4.
        """
        self._require_verifiable()
        if self.uses_v4:
            charge = await self._find_v4_charge(reference)
            if charge is not None or not self._secret_key:
                return self._from_v4_charge(charge or {}, fallback_reference=reference)
        data = await self._request(
            "GET", f"{self._base_url}/transactions/verify_by_reference",
            headers=self._v3_headers(), params={"tx_ref": reference},
        )
        return self._from_v3(data, fallback_reference=reference)

    async def verify_transaction_by_id(self, transaction_id: str) -> VerifiedTransaction:
        """By Flutterwave's own id: ``chg_...`` on v4, numeric on v3."""
        self._require_verifiable()
        if transaction_id.startswith("chg_") and self.uses_v4:
            data = await self._request(
                "GET", f"{self._v4_base_url}/charges/{transaction_id}",
                headers=await self._v4_headers(),
            )
            charge = data.get("data") if isinstance(data.get("data"), dict) else {}
            return self._from_v4_charge(charge, fallback_reference="")
        if not self._secret_key:
            raise AppError(f"{self.label} is not configured", status_code=503)
        data = await self._request(
            "GET", f"{self._base_url}/transactions/{transaction_id}/verify",
            headers=self._v3_headers(),
        )
        return self._from_v3(data, fallback_reference="")

    async def _find_v4_charge(self, reference: str) -> dict | None:
        data = await self._request(
            "GET", f"{self._v4_base_url}/charges",
            headers=await self._v4_headers(), params={"reference": reference},
        )
        charges = data.get("data")
        if not isinstance(charges, list):
            return None
        # The filter should already be exact; matching again means a lenient
        # or ignored filter can never hand us someone else's charge.
        matches = [c for c in charges if isinstance(c, dict) and c.get("reference") == reference]
        if not matches:
            return None
        # A reference retried after a failure can have several attempts.
        for charge in matches:
            if charge.get("status") == "succeeded":
                return charge
        return matches[0]

    def _from_v4_charge(self, charge: dict, fallback_reference: str) -> VerifiedTransaction:
        meta = charge.get("meta")
        return VerifiedTransaction(
            reference=charge.get("reference") or fallback_reference,
            succeeded=charge.get("status") == "succeeded",
            amount=to_decimal(charge.get("amount")),
            currency=charge.get("currency") or "NGN",
            metadata=meta if isinstance(meta, dict) else {},
            provider_transaction_id=str(charge["id"]) if charge.get("id") is not None else None,
        )

    def _from_v3(self, data: dict, fallback_reference: str) -> VerifiedTransaction:
        body = data.get("data") or {}
        return VerifiedTransaction(
            reference=body.get("tx_ref") or fallback_reference,
            succeeded=data.get("status") == "success" and body.get("status") == "successful",
            amount=to_decimal(body.get("amount")),
            currency=body.get("currency") or "NGN",
            metadata=body.get("meta") if isinstance(body.get("meta"), dict) else {},
            provider_transaction_id=str(body["id"]) if body.get("id") is not None else None,
        )

    # -- webhooks --------------------------------------------------------------

    def signature_from(self, headers: Mapping[str, str]) -> str | None:
        """v4 signs in ``flutterwave-signature``; v3 sent ``verif-hash``."""
        return headers.get(V4_SIGNATURE_HEADER) or headers.get(V3_SIGNATURE_HEADER)

    def verify_webhook(self, payload: bytes, signature: str | None) -> bool:
        if not self._webhook_hash:
            # Fail closed. Comparing against an empty secret would authenticate
            # an empty header, which is how an unconfigured deployment ends up
            # accepting forged events.
            log.warning("flutterwave_webhook_secret_not_configured")
            return False
        if not signature:
            return False
        expected = base64.b64encode(
            hmac.new(self._webhook_hash.encode(), payload, hashlib.sha256).digest()
        ).decode()
        if hmac.compare_digest(signature, expected):
            return True
        # v3 sent the secret hash itself. Flutterwave's v4 webhook page still
        # shows that comparison in its framework examples, so it stays
        # accepted: it proves knowledge of the same secret, and every event is
        # re-verified against the API before anything is granted.
        return hmac.compare_digest(signature, self._webhook_hash)

    def parse_webhook(self, payload: bytes) -> WebhookEvent:
        event = self._loads(payload)
        body = event.get("data") if isinstance(event.get("data"), dict) else {}
        # v4 names the event `type`; v3 called it `event`.
        event_type = event.get("type") or event.get("event")
        transaction_id = body.get("id")
        return WebhookEvent(
            is_payment_success=(
                event_type == "charge.completed"
                # v4 says "succeeded", v3 said "successful".
                and body.get("status") in ("succeeded", "successful")
            ),
            reference=body.get("reference") or body.get("tx_ref"),
            provider_transaction_id=str(transaction_id) if transaction_id is not None else None,
            raw_event=event_type,
        )

    def _initialization_failed(self, data: dict):
        from app.exceptions import PaymentError
        return PaymentError(data.get("message") or "Flutterwave initialization failed")
