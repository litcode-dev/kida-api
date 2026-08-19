"""The digest's safety net: the API process runs it too.

Celery beat is a separate process, and a deployment that never started one — or
started one that later died — produces exactly the symptom this exists to fix:
uploads accumulate, nothing errors, and no daily mail ever arrives. Beat is also
the only process nobody notices is missing, because it serves no traffic.

So the API, which is by definition running, checks a few times an hour whether
the digest that was due has actually been claimed, and runs it itself if not.
Claiming is a unique insert into ``digest_runs``, so beat and any number of API
replicas can all be doing this at once and the list still gets one email.

A late run is capped by CONTENT_DIGEST_CATCH_UP_HOURS: catching up an hour after
the scheduled time is a fix, but delivering "today's drops" at four in the
morning is not, so beyond the window the run is left for the next slot.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import structlog

from app.config import get_settings
from app.services import digest_run_service
from app.services.digest_sender import run_digest

log = structlog.get_logger()

_task: asyncio.Task | None = None


async def tick(now: datetime | None = None) -> None:
    """One check. Runs the digest if its slot is due, recent, and unclaimed."""
    settings = get_settings()
    if not settings.content_digest_enabled:
        return

    now = now or datetime.now(timezone.utc)
    slot = digest_run_service.due_slot(settings.content_digest_hour_utc, now)
    if now - slot > timedelta(hours=settings.content_digest_catch_up_hours):
        return

    await run_digest(trigger="api", now=now)


async def _loop(interval: int) -> None:
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the loop
            log.error("digest_scheduler.tick_failed", error=str(exc), exc_info=True)
        await asyncio.sleep(interval)


def start() -> None:
    """Begin checking, unless this deployment has turned the safety net off."""
    global _task
    settings = get_settings()
    if not settings.content_digest_scheduler_enabled:
        log.info("digest_scheduler.disabled")
        return
    if _task and not _task.done():
        return

    interval = max(settings.content_digest_scheduler_interval_seconds, 60)
    _task = asyncio.create_task(_loop(interval))
    log.info(
        "digest_scheduler.started",
        interval_seconds=interval,
        hour_utc=settings.content_digest_hour_utc,
    )


async def stop() -> None:
    global _task
    if not _task:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001 - the process is going away anyway
        log.error("digest_scheduler.stop_failed", error=str(exc))
    _task = None
    log.info("digest_scheduler.stopped")
