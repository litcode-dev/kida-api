# app/services/producer_analytics_service.py
import uuid
from decimal import Decimal
from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.loop import Loop
from app.models.drone_pad import Drone, DronePad
from app.models.drum_kit import DrumKit
from app.models.purchase import Purchase
from app.models.download import Download
from app.schemas.producer_analytics import (
    AnalyticsParams, AnalyticsItem, AnalyticsSection,
    AnalyticsSummary, TypeStats,
)

_ZERO = Decimal("0")


async def _loop_stats(
    db: AsyncSession,
    producer_id: uuid.UUID,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> tuple[dict, dict, list]:
    loops = list((await db.scalars(
        select(Loop).where(Loop.created_by == producer_id).order_by(Loop.created_at.desc())
    )).all())
    if not loops:
        return {}, {}, []
    loop_ids = [l.id for l in loops]

    eq = (
        select(
            Purchase.loop_id,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .where(Purchase.loop_id.in_(loop_ids))
        .group_by(Purchase.loop_id)
    )
    if from_dt:
        eq = eq.where(Purchase.created_at >= from_dt)
    if to_dt:
        eq = eq.where(Purchase.created_at <= to_dt)

    dq = (
        select(Download.loop_id, func.count(Download.id).label("downloads"))
        .where(Download.loop_id.in_(loop_ids))
        .group_by(Download.loop_id)
    )
    if from_dt:
        dq = dq.where(Download.downloaded_at >= from_dt)
    if to_dt:
        dq = dq.where(Download.downloaded_at <= to_dt)

    e_map = {r.loop_id: (r.earnings, r.sales) for r in (await db.execute(eq)).all()}
    d_map = {r.loop_id: r.downloads for r in (await db.execute(dq)).all()}
    return e_map, d_map, loops


async def _drone_stats(
    db: AsyncSession,
    producer_id: uuid.UUID,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> tuple[dict, dict, list]:
    drones = list((await db.scalars(
        select(Drone).where(Drone.created_by == producer_id).order_by(Drone.created_at.desc())
    )).all())
    if not drones:
        return {}, {}, []
    drone_ids = [d.id for d in drones]

    eq = (
        select(
            DronePad.drone_id,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .join(DronePad, Purchase.drone_pad_id == DronePad.id)
        .where(DronePad.drone_id.in_(drone_ids))
        .group_by(DronePad.drone_id)
    )
    if from_dt:
        eq = eq.where(Purchase.created_at >= from_dt)
    if to_dt:
        eq = eq.where(Purchase.created_at <= to_dt)

    dq = (
        select(DronePad.drone_id, func.count(Download.id).label("downloads"))
        .join(DronePad, Download.drone_pad_id == DronePad.id)
        .where(DronePad.drone_id.in_(drone_ids))
        .group_by(DronePad.drone_id)
    )
    if from_dt:
        dq = dq.where(Download.downloaded_at >= from_dt)
    if to_dt:
        dq = dq.where(Download.downloaded_at <= to_dt)

    e_map = {r.drone_id: (r.earnings, r.sales) for r in (await db.execute(eq)).all()}
    d_map = {r.drone_id: r.downloads for r in (await db.execute(dq)).all()}
    return e_map, d_map, drones


async def _kit_stats(
    db: AsyncSession,
    producer_id: uuid.UUID,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> tuple[dict, dict, list]:
    kits = list((await db.scalars(
        select(DrumKit).where(DrumKit.created_by == producer_id).order_by(DrumKit.created_at.desc())
    )).all())
    if not kits:
        return {}, {}, []
    kit_ids = [k.id for k in kits]

    eq = (
        select(
            Purchase.drum_kit_id,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .where(Purchase.drum_kit_id.in_(kit_ids))
        .group_by(Purchase.drum_kit_id)
    )
    if from_dt:
        eq = eq.where(Purchase.created_at >= from_dt)
    if to_dt:
        eq = eq.where(Purchase.created_at <= to_dt)

    dq = (
        select(Download.drum_kit_id, func.count(Download.id).label("downloads"))
        .where(Download.drum_kit_id.in_(kit_ids))
        .group_by(Download.drum_kit_id)
    )
    if from_dt:
        dq = dq.where(Download.downloaded_at >= from_dt)
    if to_dt:
        dq = dq.where(Download.downloaded_at <= to_dt)

    e_map = {r.drum_kit_id: (r.earnings, r.sales) for r in (await db.execute(eq)).all()}
    d_map = {r.drum_kit_id: r.downloads for r in (await db.execute(dq)).all()}
    return e_map, d_map, kits


def _build_loop_section(loops, e_map, d_map, page, page_size, cf_base) -> AnalyticsSection:
    items = [
        AnalyticsItem(
            id=l.id,
            title=l.title,
            thumbnail_url=f"{cf_base}/{l.thumbnail_s3_key}" if l.thumbnail_s3_key else None,
            earnings=e_map.get(l.id, (_ZERO, 0))[0],
            sales=e_map.get(l.id, (_ZERO, 0))[1],
            downloads=d_map.get(l.id, 0),
        )
        for l in loops
    ]
    start = (page - 1) * page_size
    return AnalyticsSection(items=items[start:start + page_size], total=len(items), page=page, page_size=page_size)


def _build_drone_section(drones, e_map, d_map, page, page_size, cf_base) -> AnalyticsSection:
    # Drone.thumbnail_url stores the S3 key (populated during the drones migration)
    items = [
        AnalyticsItem(
            id=d.id,
            title=d.title,
            thumbnail_url=f"{cf_base}/{d.thumbnail_url}" if d.thumbnail_url else None,
            earnings=e_map.get(d.id, (_ZERO, 0))[0],
            sales=e_map.get(d.id, (_ZERO, 0))[1],
            downloads=d_map.get(d.id, 0),
        )
        for d in drones
    ]
    start = (page - 1) * page_size
    return AnalyticsSection(items=items[start:start + page_size], total=len(items), page=page, page_size=page_size)


def _build_kit_section(kits, e_map, d_map, page, page_size, cf_base) -> AnalyticsSection:
    items = [
        AnalyticsItem(
            id=k.id,
            title=k.title,
            thumbnail_url=f"{cf_base}/{k.thumbnail_s3_key}" if k.thumbnail_s3_key else None,
            earnings=e_map.get(k.id, (_ZERO, 0))[0],
            sales=e_map.get(k.id, (_ZERO, 0))[1],
            downloads=d_map.get(k.id, 0),
        )
        for k in kits
    ]
    start = (page - 1) * page_size
    return AnalyticsSection(items=items[start:start + page_size], total=len(items), page=page, page_size=page_size)


async def get_producer_analytics(
    db: AsyncSession,
    producer_id: uuid.UUID,
    params: AnalyticsParams,
) -> dict:
    from app.config import get_settings
    cf_base = get_settings().s3_cloudfront_url.rstrip("/")
    from_dt, to_dt = params.resolve_window()

    loop_e, loop_d, loops = await _loop_stats(db, producer_id, from_dt, to_dt)
    drone_e, drone_d, drones = await _drone_stats(db, producer_id, from_dt, to_dt)
    kit_e, kit_d, kits = await _kit_stats(db, producer_id, from_dt, to_dt)

    loop_section = _build_loop_section(loops, loop_e, loop_d, params.loops_page, params.page_size, cf_base)
    drone_section = _build_drone_section(drones, drone_e, drone_d, params.drones_page, params.page_size, cf_base)
    kit_section = _build_kit_section(kits, kit_e, kit_d, params.drum_kits_page, params.page_size, cf_base)

    l_earn = sum(v[0] for v in loop_e.values())
    l_sales = sum(v[1] for v in loop_e.values())
    l_dl = sum(loop_d.values())

    dr_earn = sum(v[0] for v in drone_e.values())
    dr_sales = sum(v[1] for v in drone_e.values())
    dr_dl = sum(drone_d.values())

    k_earn = sum(v[0] for v in kit_e.values())
    k_sales = sum(v[1] for v in kit_e.values())
    k_dl = sum(kit_d.values())

    summary = AnalyticsSummary(
        total_earnings=l_earn + dr_earn + k_earn,
        total_sales=l_sales + dr_sales + k_sales,
        total_downloads=l_dl + dr_dl + k_dl,
        by_type={
            "loops": TypeStats(earnings=l_earn, sales=l_sales, downloads=l_dl),
            "drones": TypeStats(earnings=dr_earn, sales=dr_sales, downloads=dr_dl),
            "drum_kits": TypeStats(earnings=k_earn, sales=k_sales, downloads=k_dl),
        },
    )

    if from_dt and to_dt:
        period_out = {"from": from_dt.date().isoformat(), "to": to_dt.date().isoformat()}
    else:
        period_out = {"from": None, "to": None}

    return {
        "period": period_out,
        "summary": summary.model_dump(),
        "loops": loop_section.model_dump(),
        "drones": drone_section.model_dump(),
        "drum_kits": kit_section.model_dump(),
    }
