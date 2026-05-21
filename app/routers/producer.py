# app/routers/producer.py
from datetime import date
from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.middleware.auth_middleware import require_producer
from app.services.producer_analytics_service import get_producer_analytics
from app.schemas.producer_analytics import AnalyticsParams, AnalyticsPeriod
from app.schemas.common import success
from app.exceptions import AppError

router = APIRouter(prefix="/producer", tags=["producer"])


@router.get("/analytics")
async def producer_analytics(
    period: AnalyticsPeriod = Query(AnalyticsPeriod.all),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    loops_page: int = Query(1),
    drones_page: int = Query(1),
    drum_kits_page: int = Query(1),
    page_size: int = Query(20),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    try:
        params = AnalyticsParams(
            period=period,
            from_date=from_date,
            to_date=to_date,
            loops_page=loops_page,
            drones_page=drones_page,
            drum_kits_page=drum_kits_page,
            page_size=page_size,
        )
    except ValidationError as e:
        raise AppError(e.errors()[0]["msg"], status_code=422)

    data = await get_producer_analytics(db, producer.id, params)
    return success(data)
