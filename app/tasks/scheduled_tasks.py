"""Periodic housekeeping run by celery beat (schedule in celery_app.py)."""
import asyncio
from datetime import timedelta

import structlog

from app.tasks.celery_app import celery_app

log = structlog.get_logger()


@celery_app.task
def purge_unverified_accounts():
    """Delete email/password accounts left unverified past the configured TTL.

    Catches the sign-ups that never finish: typos, abandoned attempts and
    probes on addresses nobody reads. Removing them also frees the address to
    register again. Never raises — the next hourly run picks up anything this
    one missed.
    """

    async def _run():
        from app.config import get_settings
        from app.database import AsyncSessionLocal
        from app.services import auth_service

        hours = get_settings().unverified_account_ttl_hours
        async with AsyncSessionLocal() as db:
            return await auth_service.purge_unverified_accounts(
                db, timedelta(hours=hours)
            )

    try:
        result = asyncio.run(_run())
        log.info("unverified_account.purge_finished", **result)
    except Exception as exc:  # noqa: BLE001 - the next run retries
        log.error("unverified_account.purge_failed", error=str(exc))
