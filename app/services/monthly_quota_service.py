"""Monthly download allowance: 20 loops, 5 drones, 5 drum kits per calendar month.

Separate from free_tier_service, which caps FREE content over the account's
lifetime and exempts subscribers. This one is a fair-use ceiling that applies to
everybody, so it is the only limit a subscriber ever meets.

Counted per distinct item per month, not per request: taking the same loop twice
in a month costs one slot, because the app re-downloads when files go missing on
the device. Drone groups count once however many of their pads are fetched.

Periods are calendar months in UTC and reset at the first of the month; nothing
has to be swept, the period key simply changes.

Any of the three limits can be set to "unlimited" in the environment, which
turns that type off entirely — no check and no usage recorded.

Past the allowance a user may spend a purchased download credit instead of being
refused (see User.download_extra_credits). Credits are pre-paid because a card
cannot be charged inside a download request; they are shared across all three
types, so one purchase covers whichever runs out.
"""
import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import MonthlyDownloadLimitError
from app.models.monthly_download_usage import MonthlyDownloadUsage, MonthlyQuotaType
from app.models.user import User


def current_period(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def next_period_start(now: datetime | None = None) -> datetime:
    """First instant of next month in UTC — when the allowance resets."""
    now = now or datetime.now(timezone.utc)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def limit_for(item_type: MonthlyQuotaType) -> int | None:
    """The month's allowance for a type, or None when it is uncapped."""
    settings = get_settings()
    return {
        MonthlyQuotaType.loop: settings.monthly_loop_downloads,
        MonthlyQuotaType.drone: settings.monthly_drone_downloads,
        MonthlyQuotaType.drum_kit: settings.monthly_drum_kit_downloads,
    }[item_type]


def _lock_key(user_id: uuid.UUID, item_type: MonthlyQuotaType, period: str) -> int:
    """Stable 64-bit advisory-lock key for one user's allowance of one type."""
    digest = hashlib.sha256(f"{user_id}:{item_type.value}:{period}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def used(
    db: AsyncSession, user_id: uuid.UUID, item_type: MonthlyQuotaType, period: str | None = None
) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(MonthlyDownloadUsage)
        .where(
            MonthlyDownloadUsage.user_id == user_id,
            MonthlyDownloadUsage.period == (period or current_period()),
            MonthlyDownloadUsage.item_type == item_type,
        )
    )


async def quota_summary(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """What the app needs to show an allowance meter."""
    period = current_period()
    items = {}
    for item_type in MonthlyQuotaType:
        limit = limit_for(item_type)
        spent = await used(db, user_id, item_type, period)
        items[item_type.value] = {
            "used": spent,
            # null for both when uncapped — there is no number to show.
            "limit": limit,
            "remaining": None if limit is None else max(limit - spent, 0),
            "unlimited": limit is None,
        }
    credits = await db.scalar(
        select(User.download_extra_credits).where(User.id == user_id)
    )
    return {
        "period": period,
        "resets_at": next_period_start().isoformat(),
        "items": items,
        # Spent one per download once a type's allowance is gone.
        "extra_credits": credits or 0,
    }


async def enforce(
    db: AsyncSession,
    user: User,
    item_type: MonthlyQuotaType,
    item_id: str,
) -> bool:
    """Consume one slot for this item. Returns True if a paid credit was spent.

    Past the month's allowance a purchased download credit is spent instead of
    refusing; MonthlyDownloadLimitError is raised only when there are none left.

    Idempotent within the period: an item already taken this month always
    succeeds without spending another slot or credit.

    The row is flushed, not committed — it commits with the caller's transaction,
    so a failure while issuing signed URLs rolls both the slot and the credit
    back. The advisory lock is held to the end of that transaction, which
    serialises concurrent first downloads for the same user and type.
    """
    user_id = user.id
    limit = limit_for(item_type)
    if limit is None:
        # Uncapped: nothing to count, so nothing is recorded either. Usage only
        # starts accruing again if a limit is put back.
        return False

    period = current_period()

    existing = await db.scalar(
        select(MonthlyDownloadUsage.id).where(
            MonthlyDownloadUsage.user_id == user_id,
            MonthlyDownloadUsage.period == period,
            MonthlyDownloadUsage.item_type == item_type,
            MonthlyDownloadUsage.item_id == item_id,
        )
    )
    if existing:
        return False

    await db.execute(select(func.pg_advisory_xact_lock(_lock_key(user_id, item_type, period))))

    # Re-check under the lock: a concurrent request may have just taken this item.
    existing = await db.scalar(
        select(MonthlyDownloadUsage.id).where(
            MonthlyDownloadUsage.user_id == user_id,
            MonthlyDownloadUsage.period == period,
            MonthlyDownloadUsage.item_type == item_type,
            MonthlyDownloadUsage.item_id == item_id,
        )
    )
    if existing:
        return False

    paid = False
    if await used(db, user_id, item_type, period) >= limit:
        if user.download_extra_credits <= 0:
            raise MonthlyDownloadLimitError(
                item_type.value, limit, next_period_start().isoformat()
            )
        user.download_extra_credits -= 1
        paid = True

    # Recorded either way, so a paid download can be re-fetched for free like
    # any other item taken this month.
    db.add(MonthlyDownloadUsage(
        user_id=user_id, period=period, item_type=item_type, item_id=item_id,
    ))
    await db.flush()
    return paid
