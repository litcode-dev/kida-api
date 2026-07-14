"""Google Play Developer API client for auto-renewable subscriptions.

Wraps ``purchases.subscriptionsv2.get`` (read state + expiry) and the
subscription acknowledge call. Play auto-refunds purchases that are never
acknowledged, so ``verify`` acknowledges on first sight. Kept as a small class
so the HTTP surface can be mocked in tests.
"""
import json
from datetime import datetime

import httpx
import structlog

from app.config import get_settings
from app.exceptions import AppError
from app.services.google_play_service import _get_access_token
from app.services.iap_types import VerifiedSubscription

logger = structlog.get_logger()

API_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"

# SubscriptionPurchaseV2.subscriptionState -> our normalised status
_STATE_MAP = {
    "SUBSCRIPTION_STATE_ACTIVE": "active",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD": "grace",
    "SUBSCRIPTION_STATE_CANCELED": "canceled",
    "SUBSCRIPTION_STATE_ON_HOLD": "expired",
    "SUBSCRIPTION_STATE_PAUSED": "expired",
    "SUBSCRIPTION_STATE_EXPIRED": "expired",
    "SUBSCRIPTION_STATE_PENDING": "expired",
}


def _parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class PlaySubscriptions:
    def __init__(self, token_provider=None):
        # Reuse the pricing service account (androidpublisher scope). Overridable for tests.
        self._token_provider = token_provider or _get_access_token

    async def _auth_header(self) -> dict:
        token = await self._token_provider()
        return {"Authorization": f"Bearer {token}"}

    async def get_subscription(self, package_name: str, purchase_token: str) -> dict:
        """purchases.subscriptionsv2.get — returns the SubscriptionPurchaseV2 resource."""
        url = f"{API_BASE}/applications/{package_name}/purchases/subscriptionsv2/tokens/{purchase_token}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=await self._auth_header())
            if resp.status_code in (400, 404, 410):
                raise AppError("Android purchase token is invalid or expired", status_code=400)
            resp.raise_for_status()
            return resp.json()

    async def acknowledge(self, package_name: str, product_id: str, purchase_token: str) -> None:
        """Acknowledge the subscription so Play does not auto-refund it."""
        url = (
            f"{API_BASE}/applications/{package_name}/purchases/subscriptions/"
            f"{product_id}/tokens/{purchase_token}:acknowledge"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=await self._auth_header(), json={})
            if resp.status_code not in (200, 204):
                # An already-acknowledged purchase is not an error worth failing verification over.
                logger.warning(
                    "play_acknowledge_failed", status=resp.status_code, product_id=product_id
                )

    async def verify(self, receipt: str) -> VerifiedSubscription:
        settings = get_settings()
        try:
            payload = json.loads(receipt)
        except (ValueError, TypeError):
            raise AppError("Invalid Android receipt format (expected JSON string)", status_code=400)

        package_name = payload.get("packageName") or settings.android_package_name
        product_id = payload.get("productId", "")
        purchase_token = payload.get("purchaseToken", "")
        if not purchase_token:
            raise AppError("Missing purchaseToken in Android receipt", status_code=400)

        data = await self.get_subscription(package_name, purchase_token)

        status = _STATE_MAP.get(data.get("subscriptionState", ""), "expired")

        expires_at: datetime | None = None
        for item in data.get("lineItems", []):
            exp = _parse_rfc3339(item.get("expiryTime"))
            if exp and (expires_at is None or exp > expires_at):
                expires_at = exp
            if not product_id:
                product_id = item.get("productId", "")

        # Play auto-refunds unacknowledged purchases — acknowledge on first sight.
        if data.get("acknowledgementState") == "ACKNOWLEDGEMENT_STATE_PENDING" and product_id:
            await self.acknowledge(package_name, product_id, purchase_token)

        return VerifiedSubscription(
            store_transaction_id=purchase_token,
            product_id=product_id,
            expires_at=expires_at,
            status=status,
            latest_receipt=receipt,
            raw_payload=data,
        )
