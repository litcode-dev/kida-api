"""The safety net that runs the digest when beat is not there to.

A missing beat process is invisible — it serves no traffic — so the API checks
the schedule too. What matters here is that it only catches up while catching
up is still useful: a digest an hour late is a fix, one delivered at 4am is not.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import digest_scheduler


def _at(hour, minute=0):
    return datetime(2026, 8, 19, hour, minute, tzinfo=timezone.utc)


def _settings(**over):
    base = {
        "content_digest_enabled": True,
        "content_digest_hour_utc": 17,
        "content_digest_catch_up_hours": 6,
        "content_digest_scheduler_enabled": True,
        "content_digest_scheduler_interval_seconds": 300,
    }
    base.update(over)
    return type("S", (), base)()


@pytest.mark.asyncio
async def test_a_due_slot_is_run():
    with patch.object(digest_scheduler, "get_settings", return_value=_settings()), \
            patch.object(digest_scheduler, "run_digest", new=AsyncMock()) as run:
        await digest_scheduler.tick(now=_at(17, 4))

    run.assert_awaited_once()
    assert run.await_args.kwargs["trigger"] == "api"


@pytest.mark.asyncio
async def test_a_slot_missed_by_hours_is_still_caught_up():
    with patch.object(digest_scheduler, "get_settings", return_value=_settings()), \
            patch.object(digest_scheduler, "run_digest", new=AsyncMock()) as run:
        await digest_scheduler.tick(now=_at(21))

    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_slot_past_the_catch_up_window_waits_for_the_next_one():
    """Nobody wants yesterday's roundup at four in the morning."""
    with patch.object(digest_scheduler, "get_settings", return_value=_settings()), \
            patch.object(digest_scheduler, "run_digest", new=AsyncMock()) as run:
        await digest_scheduler.tick(now=_at(4))

    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_disabled_digest_is_not_scheduled():
    with patch.object(
        digest_scheduler, "get_settings",
        return_value=_settings(content_digest_enabled=False),
    ), patch.object(digest_scheduler, "run_digest", new=AsyncMock()) as run:
        await digest_scheduler.tick(now=_at(18))

    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_is_a_no_op_when_the_safety_net_is_turned_off():
    with patch.object(
        digest_scheduler, "get_settings",
        return_value=_settings(content_digest_scheduler_enabled=False),
    ):
        digest_scheduler.start()

    assert digest_scheduler._task is None


@pytest.mark.asyncio
async def test_start_runs_ticks_until_stopped():
    with patch.object(digest_scheduler, "get_settings", return_value=_settings()), \
            patch.object(digest_scheduler, "tick", new=AsyncMock()) as tick:
        digest_scheduler.start()
        assert digest_scheduler._task is not None
        # Let the loop reach its first tick before shutting it down.
        for _ in range(3):
            await asyncio.sleep(0)
        await digest_scheduler.stop()

    tick.assert_awaited()
    assert digest_scheduler._task is None
