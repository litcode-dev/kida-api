"""Google Play Developer API (androidpublisher v3) client for managed in-app products.

Used by the price sync worker to push desired_price_usd to the Play store —
the store listing is the source of truth for what the app charges. Play price
changes go live within minutes and need no review.
"""
import json
import time
from decimal import Decimal

import httpx
import structlog

from app.config import get_settings
from app.exceptions import AppError

logger = structlog.get_logger()

API_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"

# Play listing limits
MAX_TITLE_LEN = 55
MAX_DESCRIPTION_LEN = 200


def _micros(amount: Decimal | int | str) -> str:
    return str(int(Decimal(amount) * 1_000_000))


async def _get_access_token() -> str:
    """Obtain a short-lived OAuth2 access token from the Play service account key."""
    import jwt as pyjwt

    settings = get_settings()
    raw = settings.google_play_service_account_json or settings.google_service_account_json
    try:
        sa = json.loads(raw)
        client_email = sa["client_email"]
        private_key = sa["private_key"]
    except (ValueError, TypeError, KeyError):
        raise AppError("Google Play service account credentials not configured", status_code=500)

    now = int(time.time())
    claim = {
        "iss": client_email,
        "scope": SCOPE,
        "aud": OAUTH_TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    assertion = pyjwt.encode(claim, private_key, algorithm="RS256")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


def _products_url(package_name: str) -> str:
    return f"{API_BASE}/applications/{package_name}/inappproducts"


def build_product_body(sku: str, title: str, description: str, price_usd: Decimal) -> dict:
    """Managed ("managedUser" = non-consumable) product payload.

    defaultPrice is USD; autoConvertMissingPrices fills the other regions,
    and NG is pinned explicitly so the Naira price stays predictable.
    """
    settings = get_settings()
    ngn = Decimal(price_usd) * settings.price_sync_ngn_per_usd
    return {
        "packageName": settings.android_package_name,
        "sku": sku,
        "status": "active",
        "purchaseType": "managedUser",
        "defaultLanguage": "en-US",
        "listings": {
            "en-US": {
                "title": title[:MAX_TITLE_LEN],
                "description": (description or title)[:MAX_DESCRIPTION_LEN],
            }
        },
        "defaultPrice": {"priceMicros": _micros(price_usd), "currency": "USD"},
        "prices": {"NG": {"priceMicros": _micros(ngn), "currency": "NGN"}},
    }


async def upsert_product(sku: str, title: str, description: str, price_usd: Decimal) -> None:
    """Insert the managed product; on 409 (SKU already exists) update it in place."""
    settings = get_settings()
    token = await _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {"autoConvertMissingPrices": "true"}
    body = build_product_body(sku, title, description, price_usd)
    base = _products_url(settings.android_package_name)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(base, params=params, headers=headers, json=body)
        if resp.status_code == 409:
            resp = await client.put(f"{base}/{sku}", params=params, headers=headers, json=body)
        resp.raise_for_status()

    logger.info("play_product_upserted", sku=sku, price_usd=str(price_usd))


async def deactivate_product(sku: str) -> None:
    """Retire a product. SKUs are never deleted or reused — only set inactive."""
    settings = get_settings()
    token = await _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    base = _products_url(settings.android_package_name)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"{base}/{sku}",
            params={"autoConvertMissingPrices": "true"},
            headers=headers,
            json={
                "packageName": settings.android_package_name,
                "sku": sku,
                "status": "inactive",
            },
        )
        if resp.status_code == 404:
            logger.warning("play_product_not_found_on_retire", sku=sku)
            return
        resp.raise_for_status()

    logger.info("play_product_deactivated", sku=sku)
