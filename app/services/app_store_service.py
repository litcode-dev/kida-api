"""App Store Connect API client for non-consumable in-app purchases.

Creates IAP products, manages their en-US localization / review screenshot /
review submission, and applies price schedules. Apple only sells at fixed
price points, so desired_price_usd is snapped to the nearest USA price point.
New products need review before going live; price changes do not.
"""
import hashlib
import time
from decimal import Decimal
from pathlib import Path

import httpx
import structlog

from app.config import get_settings
from app.exceptions import AppError

logger = structlog.get_logger()

API_BASE = "https://api.appstoreconnect.apple.com"
JWT_AUDIENCE = "appstoreconnect-v1"
JWT_TTL_SECONDS = 15 * 60  # Apple rejects tokens with exp more than 20 minutes out
BASE_TERRITORY = "USA"
STATE_APPROVED = "APPROVED"


def make_jwt() -> str:
    """Short-lived ES256 token for the App Store Connect API."""
    import jwt as pyjwt

    settings = get_settings()
    if not (settings.app_store_issuer_id and settings.app_store_key_id and settings.app_store_private_key):
        raise AppError("App Store Connect credentials not configured", status_code=500)

    now = int(time.time())
    private_key = settings.app_store_private_key.replace("\\n", "\n")
    return pyjwt.encode(
        {
            "iss": settings.app_store_issuer_id,
            "iat": now,
            "exp": now + JWT_TTL_SECONDS,
            "aud": JWT_AUDIENCE,
        },
        private_key,
        algorithm="ES256",
        headers={"kid": settings.app_store_key_id},
    )


def _headers() -> dict:
    return {"Authorization": f"Bearer {make_jwt()}"}


def _iap_relationship(iap_id: str) -> dict:
    return {"data": {"type": "inAppPurchases", "id": iap_id}}


async def find_iap(sku: str) -> dict | None:
    """Look up an IAP by its product ID (SKU). Returns the resource dict or None."""
    settings = get_settings()
    if not settings.app_store_app_id:
        raise AppError("APP_STORE_APP_ID not configured", status_code=500)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{API_BASE}/v1/apps/{settings.app_store_app_id}/inAppPurchasesV2",
            params={"filter[productId]": sku, "limit": 1},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return data[0] if data else None


async def create_iap(sku: str, name: str) -> str:
    """Create a NON_CONSUMABLE in-app purchase. Returns the new IAP's ID."""
    settings = get_settings()
    body = {
        "data": {
            "type": "inAppPurchases",
            "attributes": {
                # Apple caps the reference name at 64 characters
                "name": name[:64],
                "productId": sku,
                "inAppPurchaseType": "NON_CONSUMABLE",
            },
            "relationships": {
                "app": {"data": {"type": "apps", "id": settings.app_store_app_id}},
            },
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{API_BASE}/v2/inAppPurchases", headers=_headers(), json=body)
        resp.raise_for_status()
        iap_id = resp.json()["data"]["id"]
    logger.info("apple_iap_created", sku=sku, iap_id=iap_id)
    return iap_id


async def ensure_localization(iap_id: str, name: str, description: str) -> None:
    """Add the en-US localization; a 409 means it already exists (fine — idempotent)."""
    body = {
        "data": {
            "type": "inAppPurchaseLocalizations",
            "attributes": {
                "locale": "en-US",
                "name": name[:30],  # display name cap
                "description": (description or name)[:45],
            },
            "relationships": {"inAppPurchaseV2": _iap_relationship(iap_id)},
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{API_BASE}/v1/inAppPurchaseLocalizations", headers=_headers(), json=body
        )
        if resp.status_code == 409:
            return
        resp.raise_for_status()


async def attach_review_screenshot(iap_id: str) -> None:
    """Attach the shared review screenshot (3-step asset upload). Idempotent.

    Skips with a warning when APP_STORE_REVIEW_SCREENSHOT_PATH is not set —
    the IAP then stays in MISSING_METADATA until one is attached manually.
    """
    settings = get_settings()
    path = settings.app_store_review_screenshot_path
    if not path or not Path(path).is_file():
        logger.warning("apple_review_screenshot_missing", iap_id=iap_id, path=path)
        return

    async with httpx.AsyncClient(timeout=60) as client:
        existing = await client.get(
            f"{API_BASE}/v2/inAppPurchases/{iap_id}/appStoreReviewScreenshot",
            headers=_headers(),
        )
        if existing.status_code == 200 and existing.json().get("data"):
            return

        image = Path(path).read_bytes()
        reserve = await client.post(
            f"{API_BASE}/v1/inAppPurchaseAppStoreReviewScreenshots",
            headers=_headers(),
            json={
                "data": {
                    "type": "inAppPurchaseAppStoreReviewScreenshots",
                    "attributes": {"fileName": Path(path).name, "fileSize": len(image)},
                    "relationships": {"inAppPurchaseV2": _iap_relationship(iap_id)},
                }
            },
        )
        reserve.raise_for_status()
        screenshot = reserve.json()["data"]

        for op in screenshot["attributes"]["uploadOperations"]:
            chunk = image[op["offset"]: op["offset"] + op["length"]]
            upload_headers = {h["name"]: h["value"] for h in op.get("requestHeaders", [])}
            upload = await client.request(op["method"], op["url"], headers=upload_headers, content=chunk)
            upload.raise_for_status()

        commit = await client.patch(
            f"{API_BASE}/v1/inAppPurchaseAppStoreReviewScreenshots/{screenshot['id']}",
            headers=_headers(),
            json={
                "data": {
                    "type": "inAppPurchaseAppStoreReviewScreenshots",
                    "id": screenshot["id"],
                    "attributes": {
                        "uploaded": True,
                        "sourceFileChecksum": hashlib.md5(image).hexdigest(),
                    },
                }
            },
        )
        commit.raise_for_status()
    logger.info("apple_review_screenshot_attached", iap_id=iap_id)


async def submit_for_review(iap_id: str) -> None:
    """Submit the IAP for App Review; 409 means a submission already exists."""
    body = {
        "data": {
            "type": "inAppPurchaseSubmissions",
            "relationships": {"inAppPurchaseV2": _iap_relationship(iap_id)},
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{API_BASE}/v1/inAppPurchaseSubmissions", headers=_headers(), json=body
        )
        if resp.status_code == 409:
            return
        resp.raise_for_status()
    logger.info("apple_iap_submitted", iap_id=iap_id)


def snap_price_point(price_points: list[dict], desired_usd: Decimal) -> tuple[str, Decimal]:
    """Pick the price point nearest desired_usd (ties go to the cheaper one).

    Apple only sells at fixed price points, so the desired price is snapped
    rather than applied exactly.
    """
    if not price_points:
        raise AppError("No USA price points available for in-app purchase", status_code=502)
    desired = Decimal(desired_usd)

    def distance(point: dict) -> tuple[Decimal, Decimal]:
        price = Decimal(point["attributes"]["customerPrice"])
        return (abs(price - desired), price)

    best = min(price_points, key=distance)
    return best["id"], Decimal(best["attributes"]["customerPrice"])


async def fetch_usa_price_points(iap_id: str) -> list[dict]:
    """All USA price points for the IAP (paginated)."""
    points: list[dict] = []
    url = f"{API_BASE}/v2/inAppPurchases/{iap_id}/pricePoints"
    params = {"filter[territory]": BASE_TERRITORY, "limit": 200}
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(url, params=params, headers=_headers())
            resp.raise_for_status()
            payload = resp.json()
            points.extend(payload.get("data", []))
            url = payload.get("links", {}).get("next")
            params = None  # the next link already carries the query string
    return points


async def set_price(iap_id: str, price_point_id: str) -> None:
    """Apply a price schedule: base territory USA, effective immediately."""
    body = {
        "data": {
            "type": "inAppPurchasePriceSchedules",
            "relationships": {
                "inAppPurchase": _iap_relationship(iap_id),
                "baseTerritory": {"data": {"type": "territories", "id": BASE_TERRITORY}},
                "manualPrices": {
                    "data": [{"type": "inAppPurchasePrices", "id": "${price}"}]
                },
            },
        },
        "included": [
            {
                "type": "inAppPurchasePrices",
                "id": "${price}",
                "attributes": {"startDate": None},
                "relationships": {
                    "inAppPurchaseV2": _iap_relationship(iap_id),
                    "inAppPurchasePricePoint": {
                        "data": {"type": "inAppPurchasePricePoints", "id": price_point_id}
                    },
                },
            }
        ],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{API_BASE}/v1/inAppPurchasePriceSchedules", headers=_headers(), json=body
        )
        resp.raise_for_status()
    logger.info("apple_price_schedule_set", iap_id=iap_id, price_point_id=price_point_id)


async def apply_nearest_price(iap_id: str, desired_usd: Decimal) -> Decimal:
    """Snap desired_usd to the nearest USA price point and schedule it. Returns the snapped price."""
    points = await fetch_usa_price_points(iap_id)
    price_point_id, snapped = snap_price_point(points, desired_usd)
    await set_price(iap_id, price_point_id)
    return snapped


async def get_iap_state(iap_id: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{API_BASE}/v2/inAppPurchases/{iap_id}", headers=_headers())
        resp.raise_for_status()
        return resp.json()["data"]["attributes"]["state"]


async def remove_from_sale(iap_id: str) -> None:
    """Retire an IAP by clearing its territory availability (the product itself is never deleted)."""
    body = {
        "data": {
            "type": "inAppPurchaseAvailabilities",
            "attributes": {"availableInNewTerritories": False},
            "relationships": {
                "inAppPurchase": _iap_relationship(iap_id),
                "availableTerritories": {"data": []},
            },
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{API_BASE}/v1/inAppPurchaseAvailabilities", headers=_headers(), json=body
        )
        resp.raise_for_status()
    logger.info("apple_iap_removed_from_sale", iap_id=iap_id)
