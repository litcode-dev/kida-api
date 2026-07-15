"""Server-side enforcement of the Kiɗa free-tier download caps.

Caps apply to FREE content only and are lifetime grants per account, stored in
download_grants. Exemptions, checked in order:

1. Active Kiɗa Premium subscription (same entitlement as GET /subscriptions/me;
   the billing grace period counts as active).
2. Per-item ownership via the per-item IAP flow (GET /purchases/me).

Cap numbers live in config (FREE_TIER_* env vars) so they can be tuned without
a deploy. Grants are race-safe: inserts are serialized per (user, item type)
with a Postgres advisory transaction lock, backed by a unique constraint.
"""
import hashlib
import uuid

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import FreeTierLimitError
from app.models.download_grant import DownloadGrant, DownloadGrantType
from app.models.drone_pad import Drone, MusicalKey
from app.models.drum_kit import DrumKit
from app.models.loop import Loop
from app.models.purchase import Purchase
from app.models.user import User
from app.services import iap_subscription_service


def cap_for(item_type: DownloadGrantType) -> int:
    settings = get_settings()
    return {
        DownloadGrantType.loop: settings.free_tier_loop_downloads,
        DownloadGrantType.drum_kit: settings.free_tier_drum_kit_downloads,
        DownloadGrantType.drone_pad: settings.free_tier_drone_pad_downloads,
        DownloadGrantType.drone_group: settings.free_tier_drone_group_downloads,
    }[item_type]


async def is_subscribed(db: AsyncSession, user_id: uuid.UUID) -> bool:
    sub = await iap_subscription_service.get_subscription(db, user_id)
    return iap_subscription_service.is_active(sub)


async def _owns(db: AsyncSession, user_id: uuid.UUID, *criteria) -> bool:
    purchase = await db.scalar(
        select(Purchase.id).where(Purchase.user_id == user_id, *criteria).limit(1)
    )
    return purchase is not None


def _lock_key(user_id: uuid.UUID, item_type: DownloadGrantType) -> int:
    """Stable 64-bit advisory-lock key for one user's grants of one item type."""
    digest = hashlib.sha256(f"{user_id}:{item_type.value}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def _find_grant(
    db: AsyncSession, user_id: uuid.UUID, item_type: DownloadGrantType, item_id: str
) -> DownloadGrant | None:
    return await db.scalar(
        select(DownloadGrant).where(
            DownloadGrant.user_id == user_id,
            DownloadGrant.item_type == item_type,
            DownloadGrant.item_id == item_id,
        )
    )


async def ensure_grant(
    db: AsyncSession,
    user_id: uuid.UUID,
    item_type: DownloadGrantType,
    item_id: str,
) -> None:
    """Idempotently consume a free-tier slot, raising FreeTierLimitError at cap.

    Re-requests of an already-granted item always succeed without consuming a
    new slot (the app re-fetches when files went missing on the device).
    The grant row is flushed but not committed — it commits (and the advisory
    lock releases) with the caller's transaction, so a failure while issuing
    signed URLs rolls the slot back.
    """
    existing = await db.scalar(
        select(DownloadGrant.id).where(
            DownloadGrant.user_id == user_id,
            DownloadGrant.item_type == item_type,
            DownloadGrant.item_id == item_id,
        )
    )
    if existing:
        return

    limit = cap_for(item_type)

    # Serialize concurrent first-time grants for this user + type so two
    # simultaneous requests cannot both pass the count check.
    await db.execute(select(func.pg_advisory_xact_lock(_lock_key(user_id, item_type))))

    # Re-check under the lock: a concurrent request may have granted this item.
    existing = await db.scalar(
        select(DownloadGrant.id).where(
            DownloadGrant.user_id == user_id,
            DownloadGrant.item_type == item_type,
            DownloadGrant.item_id == item_id,
        )
    )
    if existing:
        return

    count = await db.scalar(
        select(func.count())
        .select_from(DownloadGrant)
        .where(DownloadGrant.user_id == user_id, DownloadGrant.item_type == item_type)
    )
    if count >= limit:
        raise FreeTierLimitError(item_type.value, limit)

    db.add(DownloadGrant(user_id=user_id, item_type=item_type, item_id=item_id))
    await db.flush()


async def enforce_loop_cap(db: AsyncSession, user: User, loop: Loop) -> None:
    if not loop.is_free:
        return  # paid loops are guarded by the purchase entitlement check
    if await is_subscribed(db, user.id):
        return
    if await _owns(db, user.id, (Purchase.loop_id == loop.id) | (Purchase.item_id == loop.id)):
        return
    await ensure_grant(db, user.id, DownloadGrantType.loop, str(loop.id))


async def enforce_drum_kit_cap(db: AsyncSession, user: User, kit: DrumKit) -> None:
    if not kit.is_free:
        return  # paid kits are guarded by the purchase entitlement check
    if await is_subscribed(db, user.id):
        return
    if await _owns(db, user.id, (Purchase.drum_kit_id == kit.id) | (Purchase.item_id == kit.id)):
        return
    await ensure_grant(db, user.id, DownloadGrantType.drum_kit, str(kit.id))


def drone_pad_grant_id(drone_id: uuid.UUID, key: MusicalKey) -> str:
    """Composite grant id so the same musical pad is one grant, whether it is
    fetched via the group endpoint or the title endpoint."""
    return f"{drone_id}:{key.value}"


async def enforce_drone_download(
    db: AsyncSession, user: User, drone: Drone, key: MusicalKey | None
) -> None:
    """Gate a drone download request against the free-tier rules.

    Full-group requests (key is None) for free groups require a subscription or
    group ownership. Single-pad requests (?key=) consume one drone_pad grant.
    Paid groups are untouched — per-pad purchase checks already guard them, and
    an owned paid group is always downloadable.
    """
    if not drone.is_free:
        return
    if await is_subscribed(db, user.id):
        return

    if key is None:
        if await _owns(db, user.id, Purchase.item_id == drone.id):
            return
        await ensure_grant(db, user.id, DownloadGrantType.drone_group, str(drone.id))
        return

    pad_ids = [p.id for p in drone.pads if p.key == key]
    if await _owns(
        db,
        user.id,
        (Purchase.item_id == drone.id)
        | (Purchase.drone_pad_id.in_(pad_ids))
        | (Purchase.item_id.in_(pad_ids)),
    ):
        return
    await ensure_grant(
        db, user.id, DownloadGrantType.drone_pad, drone_pad_grant_id(drone.id, key)
    )
