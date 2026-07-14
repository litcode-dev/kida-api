"""App Store client for auto-renewable subscriptions.

Verifies the base64 receipt (the plugin's serverVerificationData) against the
legacy ``/verifyReceipt`` endpoint with the app's shared secret, reading the
latest transaction's expiry and the pending-renewal grace period. Kept as a
small class so the HTTP surface can be mocked in tests.
"""
from datetime import datetime, timezone

import httpx
import structlog

from app.config import get_settings
from app.exceptions import AppError
from app.services.iap_types import VerifiedSubscription

logger = structlog.get_logger()

PRODUCTION_URL = "https://buy.itunes.apple.com/verifyReceipt"
SANDBOX_URL = "https://sandbox.itunes.apple.com/verifyReceipt"


def _ms_to_dt(ms: str | int | None) -> datetime | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (ValueError, TypeError):
        return None


class AppStoreSubscriptions:
    def __init__(self, shared_secret: str | None = None):
        self._shared_secret = shared_secret

    def _secret(self) -> str:
        settings = get_settings()
        return self._shared_secret or settings.app_store_shared_secret or settings.apple_shared_secret

    async def _post(self, client: httpx.AsyncClient, url: str, body: dict) -> dict:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        return resp.json()

    async def verify(self, receipt: str) -> VerifiedSubscription:
        body = {
            "receipt-data": receipt,
            "password": self._secret(),
            "exclude-old-transactions": True,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            data = await self._post(client, PRODUCTION_URL, body)
            # 21007 = a sandbox receipt sent to production — retry against sandbox.
            if data.get("status") == 21007:
                data = await self._post(client, SANDBOX_URL, body)

        status_code = data.get("status")
        if status_code != 0:
            logger.warning("app_store_receipt_invalid", status=status_code)
            raise AppError(
                f"App Store receipt verification failed (status {status_code})", status_code=400
            )

        transactions = data.get("latest_receipt_info") or data.get("receipt", {}).get("in_app", [])
        if not transactions:
            raise AppError("No subscription transactions found in receipt", status_code=400)

        latest = max(
            transactions,
            key=lambda t: int(t.get("expires_date_ms") or t.get("purchase_date_ms") or 0),
        )
        original_transaction_id = latest.get("original_transaction_id") or latest.get("transaction_id")
        if not original_transaction_id:
            raise AppError("Receipt is missing a transaction id", status_code=400)
        product_id = latest.get("product_id", "")
        expires_at = _ms_to_dt(latest.get("expires_date_ms"))

        now = datetime.now(timezone.utc)
        status = "active" if expires_at and expires_at > now else "expired"

        # Grace period / cancellation live in pending_renewal_info for this original txn.
        for info in data.get("pending_renewal_info", []) or []:
            if info.get("original_transaction_id") != original_transaction_id:
                continue
            grace_end = _ms_to_dt(info.get("grace_period_expires_date_ms"))
            if grace_end and grace_end > now:
                status = "grace"
                expires_at = grace_end

        # Keep the raw response for audit but drop the bulky re-encoded receipt.
        raw = {k: v for k, v in data.items() if k != "latest_receipt"}

        return VerifiedSubscription(
            store_transaction_id=str(original_transaction_id),
            product_id=product_id,
            expires_at=expires_at,
            status=status,
            latest_receipt=data.get("latest_receipt", receipt),
            raw_payload=raw,
        )
