"""How many loop/stems requests an account may submit in a calendar month.

A free account gets one request a month; Kiɗa Premium is exempt and may submit
as many as it likes. The cap exists because every request is read and answered
by hand out of the team inbox — so a request counts as soon as it is submitted,
whatever an admin later does with it.

Nothing new is recorded to count this: loop_requests already stores who asked
and when, so the month's submissions are counted straight off that table.

Periods are calendar months in UTC and reset at the first of the month, the same
way monthly_quota_service's download allowance does — the reset is just the
period changing, there is nothing to sweep. The cap can be set to "unlimited" in
the environment (FREE_TIER_MONTHLY_LOOP_REQUESTS), which turns the check off for
everybody.
"""
import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import LoopRequestLimitError
from app.models.loop_request import LoopRequest
from app.models.user import User
from app.services import iap_subscription_service
from app.services.monthly_quota_service import UNLIMITED, current_period, next_period_start


def limit_for() -> int | None:
    """This month's allowance for a free account, or None when it is uncapped."""
    return get_settings().free_tier_monthly_loop_requests


def period_start(now: datetime | None = None) -> datetime:
    """First instant of the current month in UTC — where counting starts."""
    now = now or datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _lock_key(user_id: uuid.UUID, period: str) -> int:
    """Stable 64-bit advisory-lock key for one user's requests in one period."""
    digest = hashlib.sha256(f"loop_requests:{user_id}:{period}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def is_premium(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Same entitlement as GET /subscriptions/me — the grace period counts."""
    sub = await iap_subscription_service.get_subscription(db, user_id)
    return iap_subscription_service.is_active(sub)


async def used(db: AsyncSession, user_id: uuid.UUID, since: datetime | None = None) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(LoopRequest)
        .where(
            LoopRequest.user_id == user_id,
            LoopRequest.created_at >= (since or period_start()),
        )
    )


async def summary(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """What the app needs to show the form's state before anyone fills it in."""
    limit = limit_for()
    premium = await is_premium(db, user_id)
    spent = await used(db, user_id)
    # Premium, or a cap removed in config: both read as unlimited, and the
    # count is still reported so the app can show what was asked for this month.
    uncapped = premium or limit is None
    return {
        "period": current_period(),
        "resets_at": next_period_start().isoformat(),
        "used": spent,
        "limit": UNLIMITED if uncapped else limit,
        "remaining": UNLIMITED if uncapped else max(limit - spent, 0),
        # Branch on this rather than comparing the strings above.
        "unlimited": uncapped,
        "premium": premium,
    }


async def enforce(db: AsyncSession, user: User) -> None:
    """Refuse a submission past a free account's monthly allowance.

    The advisory lock is held to the end of the caller's transaction — which is
    the one that inserts the request — so two submissions racing each other
    cannot both read the count from before either was stored.
    """
    limit = limit_for()
    if limit is None:
        return
    if await is_premium(db, user.id):
        return

    period = current_period()
    await db.execute(select(func.pg_advisory_xact_lock(_lock_key(user.id, period))))

    if await used(db, user.id) >= limit:
        raise LoopRequestLimitError(limit, next_period_start().isoformat())
