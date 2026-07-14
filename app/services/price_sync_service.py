"""Reconciles desired_price_usd on sellable items with Google Play and the App Store.

The stores are the source of truth for IAP prices: an admin sets
desired_price_usd, the worker pushes it to both stores, and the app displays
the store's own price. Every step is idempotent and safe to retry; a failure
on one item never blocks the others.

State machine per item:
    pending → (Play upsert + Apple create-or-reprice)
            → synced                (Apple IAP already APPROVED)
            → apple_pending_review  (new IAP awaiting App Review — polled on later runs)
            → error                 (failure recorded in price_sync_error; retried after re-flip)
"""
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.drone_pad import Drone
from app.models.drum_kit import DrumKit
from app.models.loop import Loop
from app.models.price_sync import PriceSyncState
from app.services import app_store_service, google_play_service, store_sku

logger = structlog.get_logger()

# (model, SKU kind) for every sellable item type
SELLABLE_MODELS: list[tuple[type, str]] = [
    (Loop, "loop"),
    (DrumKit, "kit"),
    (Drone, "drone"),
]


def ensure_sku(item, kind: str) -> str:
    """Assign the item's immutable store SKU if it doesn't have one yet."""
    if not item.store_product_id:
        item.store_product_id = store_sku.generate_sku(kind, item.title, str(item.id))
    return item.store_product_id


def mark_price_dirty(item) -> None:
    """Flip an item back to pending so the worker re-pushes its price."""
    item.price_sync_state = PriceSyncState.pending.value
    item.price_sync_error = None
    item.price_synced_at = None


def _mark_synced(item) -> None:
    item.price_sync_state = PriceSyncState.synced.value
    item.price_synced_at = datetime.now(timezone.utc)
    item.price_sync_error = None


async def sync_item(item, kind: str) -> None:
    """Push one pending item to both stores. Idempotent — safe to re-run."""
    sku = ensure_sku(item, kind)
    title = item.title
    description = item.description or item.title
    price_usd = item.desired_price_usd

    # Google Play first: live in minutes, no review needed.
    await google_play_service.upsert_product(sku, title, description, price_usd)

    # Apple: create the IAP if it doesn't exist yet, then (re)apply the price.
    iap = await app_store_service.find_iap(sku)
    if iap is None:
        iap_id = await app_store_service.create_iap(sku, title)
        await app_store_service.ensure_localization(iap_id, title, description)
        await app_store_service.attach_review_screenshot(iap_id)
        await app_store_service.apply_nearest_price(iap_id, price_usd)
        await app_store_service.submit_for_review(iap_id)
        apple_state = None  # freshly created — App Review hasn't seen it yet
    else:
        iap_id = iap["id"]
        await app_store_service.apply_nearest_price(iap_id, price_usd)
        apple_state = iap.get("attributes", {}).get("state")

    if apple_state == app_store_service.STATE_APPROVED:
        _mark_synced(item)
    else:
        item.price_sync_state = PriceSyncState.apple_pending_review.value
        item.price_sync_error = None
    logger.info(
        "price_sync_item_done",
        kind=kind,
        item_id=str(item.id),
        sku=sku,
        state=item.price_sync_state,
    )


async def poll_apple_review(item) -> None:
    """Flip apple_pending_review → synced once App Review approves the IAP."""
    iap = await app_store_service.find_iap(item.store_product_id)
    if iap is None:
        raise RuntimeError(f"Apple IAP {item.store_product_id} not found while awaiting review")
    if iap.get("attributes", {}).get("state") == app_store_service.STATE_APPROVED:
        _mark_synced(item)


async def sync_pending(db: AsyncSession) -> dict:
    """Process every item awaiting sync. Per-item failures are recorded and skipped."""
    counts = {"synced": 0, "pending_review": 0, "errors": 0}
    for model, kind in SELLABLE_MODELS:
        result = await db.scalars(
            select(model).where(
                model.price_sync_state.in_(
                    [PriceSyncState.pending.value, PriceSyncState.apple_pending_review.value]
                ),
                model.desired_price_usd.is_not(None),
                model.is_free.is_(False),
            )
        )
        for item in result.all():
            try:
                if item.price_sync_state == PriceSyncState.pending.value:
                    await sync_item(item, kind)
                else:
                    await poll_apple_review(item)
            except Exception as e:
                logger.warning(
                    "price_sync_failed", kind=kind, item_id=str(item.id), error=str(e)
                )
                item.price_sync_state = PriceSyncState.error.value
                item.price_sync_error = str(e)[:2000]
                counts["errors"] += 1
            else:
                if item.price_sync_state == PriceSyncState.synced.value:
                    counts["synced"] += 1
                else:
                    counts["pending_review"] += 1
            await db.commit()
    logger.info("price_sync_run_done", **counts)
    return counts


async def retire_product(store_product_id: str) -> None:
    """Retire a SKU: inactive on Play, removed from sale on Apple. Never deleted."""
    await google_play_service.deactivate_product(store_product_id)
    iap = await app_store_service.find_iap(store_product_id)
    if iap is not None:
        await app_store_service.remove_from_sale(iap["id"])


async def list_sync_errors(db: AsyncSession) -> list[dict]:
    """Items whose last sync attempt failed, across all sellable types."""
    out: list[dict] = []
    for model, kind in SELLABLE_MODELS:
        result = await db.scalars(
            select(model).where(model.price_sync_state == PriceSyncState.error.value)
        )
        for item in result.all():
            out.append(
                {
                    "type": kind,
                    "id": str(item.id),
                    "title": item.title,
                    "store_product_id": item.store_product_id,
                    "desired_price_usd": str(item.desired_price_usd)
                    if item.desired_price_usd is not None
                    else None,
                    "price_sync_error": item.price_sync_error,
                }
            )
    return out
