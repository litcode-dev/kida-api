# app/services/admin_analytics_service.py
import uuid
from decimal import Decimal
from datetime import datetime
import structlog as _structlog
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.loop import Loop
from app.models.drone_pad import Drone, DronePad
from app.models.drum_kit import DrumKit
from app.models.purchase import Purchase
from app.models.download import Download
from app.models.user import User
from app.schemas.producer_analytics import AnalyticsParams, TypeStats
from app.schemas.admin_analytics import (
    PlatformRevenueSummary,
    UserGrowthStats,
    TopContentItem,
    TopProducerItem,
)

_ZERO = Decimal("0")


async def _revenue_by_type(
    db: AsyncSession,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> tuple[dict[str, tuple[Decimal, int]], dict[str, int]]:
    pairs = [
        ("loops", Purchase.loop_id, Download.loop_id),
        ("drones", Purchase.drone_pad_id, Download.drone_pad_id),
        ("drum_kits", Purchase.drum_kit_id, Download.drum_kit_id),
    ]
    earnings_sales: dict[str, tuple[Decimal, int]] = {}
    downloads: dict[str, int] = {}

    for name, p_fk, d_fk in pairs:
        pq = select(
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        ).where(p_fk.isnot(None))
        if from_dt:
            pq = pq.where(Purchase.created_at >= from_dt)
        if to_dt:
            pq = pq.where(Purchase.created_at <= to_dt)
        row = (await db.execute(pq)).one()
        earnings_sales[name] = (row.earnings, row.sales)

        # counts all download types (free, paid, re-downloads) within the time window
        dq = select(func.count(Download.id).label("cnt")).where(d_fk.isnot(None))
        if from_dt:
            dq = dq.where(Download.downloaded_at >= from_dt)
        if to_dt:
            dq = dq.where(Download.downloaded_at <= to_dt)
        dl_row = (await db.execute(dq)).one()
        downloads[name] = dl_row.cnt

    return earnings_sales, downloads


async def _revenue_by_provider(
    db: AsyncSession,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> dict[str, Decimal]:
    pq = (
        select(
            Purchase.payment_provider,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
        )
        .where(Purchase.payment_provider.isnot(None))
        .group_by(Purchase.payment_provider)
    )
    if from_dt:
        pq = pq.where(Purchase.created_at >= from_dt)
    if to_dt:
        pq = pq.where(Purchase.created_at <= to_dt)
    rows = (await db.execute(pq)).all()
    return {r.payment_provider.value: r.earnings for r in rows}


async def _user_stats(
    db: AsyncSession,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> UserGrowthStats:
    # total_users and by_role are intentionally not time-filtered (platform headcount).
    # Only new_users uses the time window (growth metric).
    total = await db.scalar(select(func.count()).select_from(User)) or 0

    new_q = select(func.count()).select_from(User)
    if from_dt:
        new_q = new_q.where(User.created_at >= from_dt)
    if to_dt:
        new_q = new_q.where(User.created_at <= to_dt)
    new_users = await db.scalar(new_q) or 0

    role_rows = (await db.execute(
        select(User.role, func.count(User.id).label("cnt")).group_by(User.role)
    )).all()
    by_role = {r.role.value: r.cnt for r in role_rows}

    return UserGrowthStats(total_users=total, new_users=new_users, by_role=by_role)


async def _top_content(
    db: AsyncSession,
    from_dt: datetime | None,
    to_dt: datetime | None,
    cf_base: str,
    limit: int = 10,
) -> list[TopContentItem]:
    items: list[TopContentItem] = []

    # Loops
    loop_join = [Purchase.loop_id == Loop.id]
    if from_dt:
        loop_join.append(Purchase.created_at >= from_dt)
    if to_dt:
        loop_join.append(Purchase.created_at <= to_dt)
    lq = (
        select(
            Loop.id,
            Loop.title,
            Loop.thumbnail_s3_key,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .outerjoin(Purchase, and_(*loop_join))
        .group_by(Loop.id, Loop.title, Loop.thumbnail_s3_key)
        .order_by(func.coalesce(func.sum(Purchase.amount_paid), _ZERO).desc())
        .limit(limit)
    )
    for r in (await db.execute(lq)).all():
        items.append(TopContentItem(
            id=r.id,
            title=r.title,
            content_type="loop",
            thumbnail_url=f"{cf_base}/{r.thumbnail_s3_key}" if r.thumbnail_s3_key else None,
            earnings=r.earnings,
            sales=r.sales,
        ))

    # Drones
    drone_join = [Purchase.drone_pad_id == DronePad.id]
    if from_dt:
        drone_join.append(Purchase.created_at >= from_dt)
    if to_dt:
        drone_join.append(Purchase.created_at <= to_dt)
    dq = (
        select(
            Drone.id,
            Drone.title,
            Drone.thumbnail_url.label("thumb"),
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .outerjoin(DronePad, DronePad.drone_id == Drone.id)
        .outerjoin(Purchase, and_(*drone_join))
        .group_by(Drone.id, Drone.title, Drone.thumbnail_url)
        .order_by(func.coalesce(func.sum(Purchase.amount_paid), _ZERO).desc())
        .limit(limit)
    )
    for r in (await db.execute(dq)).all():
        items.append(TopContentItem(
            id=r.id,
            title=r.title,
            content_type="drone",
            thumbnail_url=f"{cf_base}/{r.thumb}" if r.thumb else None,
            earnings=r.earnings,
            sales=r.sales,
        ))

    # Drum kits
    kit_join = [Purchase.drum_kit_id == DrumKit.id]
    if from_dt:
        kit_join.append(Purchase.created_at >= from_dt)
    if to_dt:
        kit_join.append(Purchase.created_at <= to_dt)
    kq = (
        select(
            DrumKit.id,
            DrumKit.title,
            DrumKit.thumbnail_s3_key,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .outerjoin(Purchase, and_(*kit_join))
        .group_by(DrumKit.id, DrumKit.title, DrumKit.thumbnail_s3_key)
        .order_by(func.coalesce(func.sum(Purchase.amount_paid), _ZERO).desc())
        .limit(limit)
    )
    for r in (await db.execute(kq)).all():
        items.append(TopContentItem(
            id=r.id,
            title=r.title,
            content_type="drum_kit",
            thumbnail_url=f"{cf_base}/{r.thumbnail_s3_key}" if r.thumbnail_s3_key else None,
            earnings=r.earnings,
            sales=r.sales,
        ))

    items.sort(key=lambda x: x.earnings, reverse=True)
    return items[:limit]


async def _top_producers(
    db: AsyncSession,
    from_dt: datetime | None,
    to_dt: datetime | None,
    limit: int = 10,
) -> list[TopProducerItem]:
    totals: dict[uuid.UUID, tuple[Decimal, int]] = {}

    # Loop purchases → producer
    lq = (
        select(
            Loop.created_by,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .join(Purchase, Purchase.loop_id == Loop.id)
        .group_by(Loop.created_by)
    )
    if from_dt:
        lq = lq.where(Purchase.created_at >= from_dt)
    if to_dt:
        lq = lq.where(Purchase.created_at <= to_dt)
    for r in (await db.execute(lq)).all():
        e, s = totals.get(r.created_by, (_ZERO, 0))
        totals[r.created_by] = (e + r.earnings, s + r.sales)

    # Drone purchases → producer
    dq = (
        select(
            Drone.created_by,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .join(DronePad, DronePad.drone_id == Drone.id)
        .join(Purchase, Purchase.drone_pad_id == DronePad.id)
        .group_by(Drone.created_by)
    )
    if from_dt:
        dq = dq.where(Purchase.created_at >= from_dt)
    if to_dt:
        dq = dq.where(Purchase.created_at <= to_dt)
    for r in (await db.execute(dq)).all():
        e, s = totals.get(r.created_by, (_ZERO, 0))
        totals[r.created_by] = (e + r.earnings, s + r.sales)

    # Drum kit purchases → producer
    kq = (
        select(
            DrumKit.created_by,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .join(Purchase, Purchase.drum_kit_id == DrumKit.id)
        .group_by(DrumKit.created_by)
    )
    if from_dt:
        kq = kq.where(Purchase.created_at >= from_dt)
    if to_dt:
        kq = kq.where(Purchase.created_at <= to_dt)
    for r in (await db.execute(kq)).all():
        e, s = totals.get(r.created_by, (_ZERO, 0))
        totals[r.created_by] = (e + r.earnings, s + r.sales)

    if not totals:
        return []

    sorted_ids = sorted(totals.keys(), key=lambda pid: totals[pid][0], reverse=True)[:limit]
    users = (await db.scalars(select(User).where(User.id.in_(sorted_ids)))).all()
    user_map = {u.id: u for u in users}

    result = []
    for pid in sorted_ids:
        if pid not in user_map:
            _structlog.get_logger().warning(
                "top_producers_missing_user",
                producer_id=str(pid),
            )
            continue
        result.append(TopProducerItem(
            id=pid,
            full_name=user_map[pid].full_name,
            email=user_map[pid].email,
            total_earnings=totals[pid][0],
            total_sales=totals[pid][1],
        ))
    return result


async def get_platform_analytics(db: AsyncSession, params: AnalyticsParams) -> dict:
    from app.config import get_settings
    cf_base = (get_settings().s3_cloudfront_url or "").rstrip("/")
    from_dt, to_dt = params.resolve_window()

    earnings_sales, dl_by_type = await _revenue_by_type(db, from_dt, to_dt)
    by_provider = await _revenue_by_provider(db, from_dt, to_dt)
    user_stats = await _user_stats(db, from_dt, to_dt)
    top_content = await _top_content(db, from_dt, to_dt, cf_base)
    top_producers = await _top_producers(db, from_dt, to_dt)

    revenue = PlatformRevenueSummary(
        total_earnings=sum(v[0] for v in earnings_sales.values()),
        total_sales=sum(v[1] for v in earnings_sales.values()),
        by_type={
            name: TypeStats(earnings=v[0], sales=v[1], downloads=dl_by_type.get(name, 0))
            for name, v in earnings_sales.items()
        },
        by_provider=by_provider,
    )

    if from_dt and to_dt:
        period_out = {"from": from_dt.date().isoformat(), "to": to_dt.date().isoformat()}
    else:
        period_out = {"from": None, "to": None}

    return {
        "period": period_out,
        "revenue": revenue.model_dump(),
        "users": user_stats.model_dump(),
        "top_content": [item.model_dump() for item in top_content],
        "top_producers": [item.model_dump() for item in top_producers],
    }
