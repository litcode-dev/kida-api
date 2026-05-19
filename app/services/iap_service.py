import httpx
import structlog
from app.config import get_settings
from app.exceptions import AppError

logger = structlog.get_logger()

APPLE_PRODUCTION_URL = "https://buy.itunes.apple.com/verifyReceipt"
APPLE_SANDBOX_URL = "https://sandbox.itunes.apple.com/verifyReceipt"


async def verify_apple_receipt(receipt: str) -> str:
    """Verify an iOS receipt with Apple and return the transaction ID."""
    settings = get_settings()
    payload = {
        "receipt-data": receipt,
        "password": settings.apple_shared_secret,
        "exclude-old-transactions": True,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(APPLE_PRODUCTION_URL, json=payload)
        response.raise_for_status()
        data = response.json()

        # 21007 = receipt is from sandbox — retry against sandbox
        if data.get("status") == 21007:
            response = await client.post(APPLE_SANDBOX_URL, json=payload)
            response.raise_for_status()
            data = response.json()

    status = data.get("status")
    if status != 0:
        logger.warning("apple_receipt_invalid", status=status)
        raise AppError(402, f"Apple receipt verification failed (status {status})")

    receipts = data.get("receipt", {}).get("in_app", [])
    if not receipts:
        raise AppError(402, "No in-app purchase found in receipt")

    # Return the most recent transaction ID
    latest = max(receipts, key=lambda r: r.get("purchase_date_ms", "0"))
    return latest["transaction_id"]


async def verify_google_receipt(receipt: str) -> str:
    """Verify an Android purchase token with Google Play and return the order ID."""
    settings = get_settings()

    # receipt from Flutter is JSON: {"packageName":"...","productId":"...","purchaseToken":"..."}
    import json
    try:
        payload = json.loads(receipt)
    except Exception:
        raise AppError(402, "Invalid Android receipt format")

    package_name = payload.get("packageName", settings.android_package_name)
    product_id = payload.get("productId", "")
    purchase_token = payload.get("purchaseToken", "")

    if not purchase_token:
        raise AppError(402, "Missing purchaseToken in Android receipt")

    # Use Google Play Developer API via service account OAuth
    access_token = await _get_google_access_token(settings)

    url = (
        f"https://androidpublisher.googleapis.com/androidpublisher/v3"
        f"/applications/{package_name}/purchases/products"
        f"/{product_id}/tokens/{purchase_token}"
    )

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 404:
            raise AppError(402, "Android purchase token not found")
        response.raise_for_status()
        data = response.json()

    # purchaseState: 0 = purchased, 1 = canceled
    if data.get("purchaseState") != 0:
        raise AppError(402, f"Android purchase not in purchased state: {data.get('purchaseState')}")

    return data.get("orderId", purchase_token)


async def _get_google_access_token(settings) -> str:
    """Obtain a short-lived OAuth2 access token from a Google service account JSON key."""
    import json, time
    import jwt as pyjwt  # pip install PyJWT

    try:
        sa = json.loads(settings.google_service_account_json)
    except Exception:
        raise AppError(500, "Google service account credentials not configured")

    now = int(time.time())
    claim = {
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/androidpublisher",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }

    token = pyjwt.encode(claim, sa["private_key"], algorithm="RS256")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": token,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
