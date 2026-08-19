"""Claiming a digest run, and the record of what happened to it.

The daily digest has more than one thing that can start it — celery beat, the
API's own scheduler, an admin pressing the button — and exactly one of them may
win any given day. That is what :func:`claim` is for: the insert into
``digest_runs`` is the lock, because ``run_key`` is unique. A process that
loses the race gets ``None`` back and does nothing.

Keeping the slot arithmetic here (rather than in the caller) means the API
scheduler, the beat task and the diagnostic script all agree on which run is
due, which is the whole point of a catch-up.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.digest_run import DigestRun, DigestRunStatus


def due_slot(hour: int, now: datetime | None = None) -> datetime:
    """The most recent moment the digest was due, at or before ``now``."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return today if today <= now else today - timedelta(days=1)


def next_slot(hour: int, now: datetime | None = None) -> datetime:
    return due_slot(hour, now) + timedelta(days=1)


def slot_key(moment: datetime) -> str:
    """The unique key for a scheduled slot, e.g. "2026-08-19T17"."""
    return f"{moment.astimezone(timezone.utc):%Y-%m-%dT%H}"


def manual_key(now: datetime | None = None) -> str:
    """Key for a run somebody asked for by hand.

    Timestamped to the second so it never collides with — or consumes — the
    day's scheduled slot: pressing "send now" at noon must not stop the 17:00
    digest from running.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"manual:{now:%Y-%m-%dT%H:%M:%S}"


async def claim(
    db: AsyncSession, *, key: str, scheduled_for: datetime, trigger: str
) -> DigestRun | None:
    """Take ownership of a run, or return None if somebody already has it.

    ON CONFLICT DO NOTHING makes this a single atomic statement: no advisory
    lock, no Redis, and safe across however many API replicas and workers are
    running.
    """
    stmt = (
        insert(DigestRun)
        .values(
            id=uuid.uuid4(),
            run_key=key,
            scheduled_for=scheduled_for,
            trigger=trigger,
            status=DigestRunStatus.running,
            started_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(index_elements=["run_key"])
        .returning(DigestRun.id)
    )
    run_id = (await db.execute(stmt)).scalar_one_or_none()
    await db.commit()
    if run_id is None:
        return None
    return await db.get(DigestRun, run_id)


async def finish(
    db: AsyncSession,
    run: DigestRun,
    status: str,
    *,
    items: int = 0,
    recipients: int = 0,
    sent: int = 0,
    failed: int = 0,
    claimed_ids: dict[str, list[str]] | None = None,
    detail: str | None = None,
) -> DigestRun:
    run.status = status
    run.finished_at = datetime.now(timezone.utc)
    run.items = items
    run.recipients = recipients
    run.sent = sent
    run.failed = failed
    if claimed_ids is not None:
        run.claimed_ids = claimed_ids
    run.detail = detail
    db.add(run)
    await db.commit()
    return run


async def recent(db: AsyncSession, limit: int = 10) -> list[DigestRun]:
    rows = await db.scalars(
        select(DigestRun).order_by(DigestRun.started_at.desc()).limit(limit)
    )
    return list(rows.all())


async def find_by_key(db: AsyncSession, key: str) -> DigestRun | None:
    return await db.scalar(select(DigestRun).where(DigestRun.run_key == key))
