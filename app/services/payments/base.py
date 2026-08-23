"""The contract every payment gateway implements.

Each provider has its own vocabulary — Paystack counts in kobo and calls the
identifier a reference, Flutterwave counts in naira and calls it a tx_ref,
Stripe counts in cents and hands back a session id — so the point of this
module is to state the two or three things the application actually needs and
let each adapter do the translating. Callers work in major currency units and
in our own reference, and never branch on which provider they were given.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import ClassVar

import httpx
import structlog

from app.exceptions import AppError, PaymentError
from app.models.purchase import PaymentProvider

log = structlog.get_logger()

# Outbound calls to a gateway are in a user's request path, so they get a
# timeout rather than httpx's default.
GATEWAY_TIMEOUT = 15.0


@dataclass(frozen=True)
class CheckoutSession:
    """Where to send the payer, and the reference we will see again later."""

    checkout_url: str
    reference: str


@dataclass(frozen=True)
class VerifiedTransaction:
    """A gateway's own answer about a transaction — the authoritative one.

    ``metadata`` is what we attached at checkout, read back from the gateway
    rather than from a webhook body, so a caller can trust it.
    """

    reference: str
    succeeded: bool
    amount: Decimal
    currency: str
    metadata: dict = field(default_factory=dict)
    provider_transaction_id: str | None = None


@dataclass(frozen=True)
class WebhookEvent:
    """A payment notification, normalized across providers.

    ``reference`` is ours; everything else is only a hint. Deliberately thin:
    a webhook is untrusted input even once its signature checks out, so the
    caller is expected to confirm it with :meth:`PaymentGateway.verify_transaction`
    before it grants anything.
    """

    is_payment_success: bool
    reference: str | None = None
    provider_transaction_id: str | None = None
    raw_event: str | None = None


class PaymentGateway(ABC):
    """One payment provider, behind the same interface as all the others."""

    #: Value stored on Purchase.payment_provider.
    provider: ClassVar[PaymentProvider]
    #: Header carrying the webhook signature, so a route can stay generic.
    signature_header: ClassVar[str]
    #: Human name for messages.
    label: ClassVar[str]

    # -- configuration -------------------------------------------------------

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this gateway has the credentials it needs."""

    def require_configured(self) -> None:
        if not self.is_configured:
            raise AppError(f"{self.label} is not configured", status_code=503)

    # -- payments ------------------------------------------------------------

    @abstractmethod
    async def create_checkout(
        self,
        *,
        amount: Decimal,
        currency: str,
        reference: str,
        email: str,
        description: str,
        metadata: dict,
    ) -> CheckoutSession:
        """Start a payment. ``amount`` is in major units (naira, dollars)."""

    @abstractmethod
    async def verify_transaction(self, reference: str) -> VerifiedTransaction:
        """Ask the gateway what really happened to a transaction."""

    # -- webhooks ------------------------------------------------------------

    @abstractmethod
    def verify_webhook(self, payload: bytes, signature: str | None) -> bool:
        """Whether this body genuinely came from the gateway.

        Implementations must fail closed: no signature, or no configured
        secret to check it against, is a rejection. An unconfigured gateway
        that accepts anything is an open door, not a convenience.
        """

    @abstractmethod
    def parse_webhook(self, payload: bytes) -> WebhookEvent:
        """Normalize a verified webhook body. Never raises on odd input."""

    # -- helpers for adapters ------------------------------------------------

    async def _request(
        self, method: str, url: str, *, headers: dict, **kwargs
    ) -> dict:
        """Call the gateway and return parsed JSON, or raise something defined.

        Adapters share this so one provider's outage cannot arrive as an
        unhandled httpx error: transport failures become 503 through the
        service-unavailable handler, and a refusal becomes a PaymentError.
        """
        async with httpx.AsyncClient(timeout=GATEWAY_TIMEOUT) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)

        if resp.status_code >= 500:
            log.warning(
                "gateway_upstream_error",
                provider=self.provider.value,
                status=resp.status_code,
                body=resp.text[:300],
            )
            raise AppError(
                f"{self.label} could not process this right now. Please try again.",
                status_code=502,
            )
        try:
            data = resp.json()
        except ValueError:
            log.warning(
                "gateway_unreadable_response",
                provider=self.provider.value,
                status=resp.status_code,
                body=resp.text[:300],
            )
            raise AppError(f"{self.label} returned an unreadable response", status_code=502)

        if resp.status_code >= 400:
            message = _first_message(data) or f"{self.label} rejected the request"
            log.info(
                "gateway_rejected",
                provider=self.provider.value,
                status=resp.status_code,
                detail=message,
            )
            raise PaymentError(message)
        if not isinstance(data, dict):
            raise AppError(f"{self.label} returned an unexpected response", status_code=502)
        return data

    @staticmethod
    def _loads(payload: bytes) -> dict:
        """Parse a webhook body without letting malformed JSON escape."""
        try:
            event = json.loads(payload)
        except (ValueError, TypeError):
            return {}
        return event if isinstance(event, dict) else {}


def to_decimal(value, default: str = "0") -> Decimal:
    """Money from a gateway, whatever shape it arrived in."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _first_message(data) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("message", "error_description", "detail"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value[:200]
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message[:200]
    if isinstance(error, str) and error:
        return error[:200]
    return None
