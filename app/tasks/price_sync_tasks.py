import asyncio
from app.tasks.celery_app import celery_app


@celery_app.task
def reconcile_store_prices():
    """Push pending desired_price_usd changes to Google Play and the App Store."""
    async def _run():
        from app.database import AsyncSessionLocal
        from app.services import price_sync_service

        async with AsyncSessionLocal() as db:
            await price_sync_service.sync_pending(db)
    asyncio.run(_run())
