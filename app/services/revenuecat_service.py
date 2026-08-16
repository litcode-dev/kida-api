"""RevenueCat integration for Kiɗa Premium subscriptions.

RevenueCat is the *trusted* source of truth for store subscriptions: it does the
server-side receipt validation with Apple/Google that the client-trusted
``iap_subscription_service.verify_and_upsert`` path deliberately skips, so from
RevenueCat we get the real ``expires_date``, renewals, cancellations and billing
grace periods instead of assuming them from a product-id naming convention.

Two surfaces feed the same ``IapSubscription`` row, both normalized here into a
:class:`RevenueCatEntitlement`:

* **Webhooks** — RevenueCat POSTs a lifecycle event (INITIAL_PURCHASE, RENEWAL,
  CANCELLATION, EXPIRATION, BILLING_ISSUE, ...) to the app, authenticated with a
  shared Authorization header. See :func:`verify_webhook_auth` /
  :func:`parse_webhook_event`.
* **REST pull** — given an ``app_user_id`` we fetch the subscriber's current
  entitlements on demand. See :class:`RevenueCatClient` / :func:`parse_subscriber`.

Identity: the app calls ``Purchases.logIn(user.id)`` so RevenueCat's
``app_user_id`` equals our ``user_id`` (a UUID string), mapping 1:1 to a row.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
import structlog

from app.config import get_settings
from app.exceptions import AppError
from app.models.iap_subscription import IapPlatform, IapSubscriptionStatus

logger = structlog.get_logger()

# RevenueCat ``store`` values → our platform enum. Amazon/Stripe/promotional and
# other non-mobile stores have no ios/android platform and stay null.
_STORE_TO_PLATFORM = {
    "APP_STORE": IapPlatform.ios,
    "MAC_APP_STORE": IapPlatform.ios,
    "PLAY_STORE": IapPlatform.android,
}

# Webhook event types that end entitlement outright, regardless of timestamps.
_EXPIRING_EVENTS = {"EXPIRATION"}
# Webhook event types that signal the subscription is in a billing grace period.
_BILLING_GRACE_EVENTS = {"BILLING_ISSUE"}
# Webhook event types we intentionally ignore (no entitlement change to apply).
_IGNORED_EVENTS = {"TEST"}


@dataclass(frozen=True)
class RevenueCatEntitlement:
    """A subscriber's Kiɗa Premium state, normalized from a webhook or REST pull.

    ``status`` and ``expires_at`` are computed to be consumed directly by
    ``iap_subscription_service`` (whose ``is_active`` grants entitlement while the
    status is active/grace and ``expires_at`` is in the future).
    """

    app_user_id: str
    product_id: str | None
    status: IapSubscriptionStatus
    expires_at: datetime | None
    platform: IapPlatform | None
    store_transaction_id: str | None


def _to_dt(value) -> datetime | None:
    """Parse a RevenueCat timestamp: epoch-ms (webhooks) or ISO-8601 (REST)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        if v.isdigit():
            return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
        try:
            # RevenueCat renders "...Z"; fromisoformat handles +00:00.
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _status_from_expiry(
    expires_at: datetime | None,
    grace_expires_at: datetime | None,
    *,
    now: datetime,
) -> IapSubscriptionStatus:
    """Derive status from the effective expiry.

    A subscriber in a billing grace period keeps entitlement (``grace``); an
    active/canceled-but-not-yet-expired subscriber is ``active`` (turning off
    auto-renew does not remove access until the paid period ends); anything past
    its effective expiry is ``expired``.
    """
    if grace_expires_at and grace_expires_at > now:
        return IapSubscriptionStatus.grace
    if expires_at and expires_at > now:
        return IapSubscriptionStatus.active
    return IapSubscriptionStatus.expired


# --- Webhook authentication --------------------------------------------------


def verify_webhook_auth(authorization: str | None) -> bool:
    """Constant-time check of the webhook's Authorization header.

    Fails closed: if no expected value is configured, every request is rejected
    so a misconfiguration cannot leave the endpoint open. Mirrors the HMAC
    signature check used for the Paystack webhook.
    """
    settings = get_settings()
    expected = settings.revenuecat_webhook_auth_header
    if not expected:
        logger.warning("revenuecat_webhook_auth_not_configured")
        return False
    if not authorization:
        return False
    return hmac.compare_digest(authorization, expected)


# --- Webhook event parsing ---------------------------------------------------


def parse_webhook_event(payload: dict) -> RevenueCatEntitlement | None:
    """Normalize a RevenueCat webhook body into a :class:`RevenueCatEntitlement`.

    Returns ``None`` for events that carry no entitlement change to apply (TEST
    pings, events for an entitlement other than the configured premium one, or a
    body missing the app_user_id). Raises :class:`AppError` on a malformed body.
    """
    if not isinstance(payload, dict):
        raise AppError("Invalid RevenueCat webhook body", status_code=400)

    event = payload.get("event")
    if not isinstance(event, dict):
        raise AppError("Missing RevenueCat event", status_code=400)

    event_type = (event.get("type") or "").upper()
    if event_type in _IGNORED_EVENTS:
        logger.info("revenuecat_event_ignored", event_type=event_type)
        return None

    app_user_id = event.get("app_user_id") or event.get("original_app_user_id")
    if not app_user_id:
        logger.warning("revenuecat_event_no_app_user_id", event_type=event_type)
        return None

    # Only act on events that touch the Kiɗa Premium entitlement. RevenueCat omits
    # entitlement_ids on some event types; when absent we fall through and trust
    # the product id, since the app only sells premium products.
    settings = get_settings()
    entitlement_ids = event.get("entitlement_ids")
    if entitlement_ids and settings.revenuecat_entitlement_id not in entitlement_ids:
        logger.info(
            "revenuecat_event_other_entitlement",
            event_type=event_type,
            entitlement_ids=entitlement_ids,
        )
        return None

    now = datetime.now(timezone.utc)
    expires_at = _to_dt(event.get("expiration_at_ms"))
    grace_expires_at = _to_dt(event.get("grace_period_expiration_at_ms"))

    if event_type in _EXPIRING_EVENTS:
        status = IapSubscriptionStatus.expired
    elif event_type in _BILLING_GRACE_EVENTS:
        status = IapSubscriptionStatus.grace
        # A billing issue keeps access through the grace window (or the paid
        # period if no explicit grace window was supplied).
        expires_at = grace_expires_at or expires_at
    else:
        status = _status_from_expiry(expires_at, grace_expires_at, now=now)
        if grace_expires_at and grace_expires_at > (expires_at or now):
            expires_at = grace_expires_at

    return RevenueCatEntitlement(
        app_user_id=str(app_user_id),
        product_id=event.get("product_id"),
        status=status,
        expires_at=expires_at,
        platform=_STORE_TO_PLATFORM.get((event.get("store") or "").upper()),
        store_transaction_id=(
            event.get("original_transaction_id") or event.get("transaction_id")
        ),
    )


# --- REST subscriber pull ----------------------------------------------------


def parse_subscriber(app_user_id: str, payload: dict) -> RevenueCatEntitlement | None:
    """Normalize a ``GET /subscribers/{id}`` response into an entitlement.

    Returns ``None`` when the subscriber has no Kiɗa Premium entitlement at all
    (they have never subscribed). An expired entitlement is still returned, with
    ``status = expired``, so callers can reconcile a lapsed subscription.
    """
    subscriber = (payload or {}).get("subscriber") or {}
    entitlement_id = get_settings().revenuecat_entitlement_id
    ent = (subscriber.get("entitlements") or {}).get(entitlement_id)
    if not ent:
        return None

    product_id = ent.get("product_identifier")
    sub = (subscriber.get("subscriptions") or {}).get(product_id) or {}

    now = datetime.now(timezone.utc)
    expires_at = _to_dt(ent.get("expires_date"))
    grace_expires_at = _to_dt(
        ent.get("grace_period_expires_date") or sub.get("grace_period_expires_date")
    )
    # An unresolved billing issue while still inside the paid period is a grace
    # period even when RevenueCat did not populate a grace expiry date.
    if sub.get("billing_issues_detected_at") and not grace_expires_at:
        if expires_at and expires_at > now:
            grace_expires_at = expires_at

    status = _status_from_expiry(expires_at, grace_expires_at, now=now)

    return RevenueCatEntitlement(
        app_user_id=str(app_user_id),
        product_id=product_id,
        status=status,
        expires_at=grace_expires_at if status == IapSubscriptionStatus.grace else expires_at,
        platform=_STORE_TO_PLATFORM.get((sub.get("store") or "").upper()),
        store_transaction_id=sub.get("store_transaction_id"),
    )


class RevenueCatClient:
    """Thin async client for RevenueCat's REST API.

    Kept as a small class so the HTTP surface can be mocked in tests, matching
    ``AppStoreSubscriptions`` / ``google_play_service``.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        settings = get_settings()
        self._api_key = api_key or settings.revenuecat_api_key
        self._base_url = (base_url or settings.revenuecat_base_url).rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _get(self, path: str) -> dict:
        if not self._api_key:
            raise AppError("RevenueCat is not configured", status_code=503)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}{path}",
                headers=self._headers(),
                timeout=15.0,
            )
        if resp.status_code == 404:
            # Unknown subscriber — no purchases yet.
            return {"subscriber": {}}
        if resp.status_code >= 400:
            logger.warning(
                "revenuecat_api_error", status=resp.status_code, body=resp.text[:500]
            )
            raise AppError("RevenueCat lookup failed", status_code=502)
        return resp.json()

    async def get_subscriber(self, app_user_id: str) -> dict:
        """Fetch a subscriber's entitlements. Returns the raw API response."""
        return await self._get(f"/subscribers/{app_user_id}")

    async def fetch_entitlement(self, app_user_id: str) -> RevenueCatEntitlement | None:
        """Fetch and normalize a subscriber's Kiɗa Premium entitlement."""
        payload = await self.get_subscriber(app_user_id)
        return parse_subscriber(app_user_id, payload)

    async def delete_subscriber(self, app_user_id: str) -> bool:
        """Permanently delete a subscriber from RevenueCat.

        ``DELETE /v1/subscribers/{app_user_id}``. Returns True when RevenueCat
        no longer holds the subscriber — which includes a 404, because "we never
        had them" and "we no longer have them" are the same end state and a
        retry of an already-successful delete must not look like a failure.

        Returns False on anything retryable (5xx, rate limit, network). Raises
        :class:`AppError` only for a 4xx that will never succeed on retry, so
        the caller can stop trying rather than loop forever.

        Note this deletes RevenueCat's *record* of the customer. It does not
        cancel or refund an active store subscription — only Apple or Google can
        do that, and the user has to do it from their store account.
        """
        if not self._api_key:
            logger.info("revenuecat_delete_skipped", reason="not configured")
            raise AppError("RevenueCat is not configured", status_code=503)

        url = f"{self._base_url}/subscribers/{quote(app_user_id, safe='')}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.delete(url, headers=self._headers(), timeout=15.0)
        except httpx.HTTPError as exc:
            logger.warning("revenuecat_delete_transport_error", error=str(exc))
            return False

        if resp.status_code in (200, 202, 204, 404):
            logger.info("revenuecat_subscriber_deleted", status=resp.status_code)
            return True
        if resp.status_code == 429 or resp.status_code >= 500:
            logger.warning("revenuecat_delete_retryable", status=resp.status_code)
            return False
        logger.error(
            "revenuecat_delete_failed", status=resp.status_code, body=resp.text[:300]
        )
        raise AppError(
            f"RevenueCat rejected the delete ({resp.status_code})", status_code=502
        )
