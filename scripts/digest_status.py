"""Why the daily new-content digest did — or did not — go out.

The digest is a once-a-day sweep: at CONTENT_DIGEST_HOUR_UTC it collects
everything that is live and not yet announced, stamps each item, and sends one
email. When something is uploaded and no mail arrives, the cause is always one
of a handful of things, and none of them are visible from outside the database:

  * beat never fired, so the sweep did not run at all
  * the item is not "ready" yet, so the sweep skipped it
  * the item was uploaded after the hour, and goes out on the next run
  * the item predates the digest deploy and was backfilled as already announced
  * the sweep ran and claimed the item, but the send then failed
  * the mail backend has no credentials, so every message was skipped while the
    run still logged sent=N, failed=0

This prints all six, plus what the next digest currently holds.

It also answers "is beat even running?" from the database alone, by checking
the marks beat's other scheduled jobs leave behind — which is the only way to
ask on a hosted platform where there is no container to inspect.

Nothing here writes to the database unless --release or --send-now is passed.

Run:
    python -m scripts.digest_status
    python -m scripts.digest_status --release loop:<uuid> --release drum_kit:<uuid>
    python -m scripts.digest_status --send-now --yes
"""
import argparse
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select, text, update

from app.config import get_settings
from app.database import AsyncSessionLocal, engine
from app.models.download import Download
from app.models.drone_pad import Drone, DronePad
from app.models.drum_kit import DrumKit, DrumSample
from app.models.loop import Loop
from app.models.stem_pack import Stem, StemPack
from app.schemas.broadcast import BroadcastAudience
from app.services import broadcast_service, content_digest_service

# Lagos, which is what the default 17:00 UTC was chosen against.
WAT = timezone(timedelta(hours=1), "WAT")

# (key, label, parent model, child model, parent fk on the child)
# Loops have no children — their own status column is the whole story.
CHILD_TYPES = [
    ("drum_kit", "Drum kits", DrumKit, DrumSample, DrumSample.drum_kit_id),
    ("drone_pad", "Drones", Drone, DronePad, DronePad.drone_id),
    ("stem_pack", "Stem packs", StemPack, Stem, Stem.stem_pack_id),
]

RELEASABLE = {
    "loop": Loop,
    "drum_kit": DrumKit,
    "drone_pad": Drone,
    "stem_pack": StemPack,
}


def _both_zones(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M} UTC  ({moment.astimezone(WAT):%H:%M} WAT)"


def _run_window(hour: int, now: datetime) -> tuple[datetime, datetime]:
    """The most recent scheduled fire time, and the next one."""
    today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if today <= now:
        return today, today + timedelta(days=1)
    return today - timedelta(days=1), today


async def _digest_columns(db) -> set[str]:
    """Which of the digest's tables actually have announced_at in the database.

    A missing column means migration o4s25n06o9p7 never ran, and the sweep is
    erroring out every time rather than sending nothing.
    """
    rows = await db.scalars(text(
        "SELECT table_name FROM information_schema.columns "
        "WHERE column_name = 'announced_at'"
    ))
    return set(rows.all())


async def _child_health(db, child_model, parent_fk) -> dict[uuid.UUID, tuple[int, int]]:
    """(children, unfinished children) per parent, in one grouped query."""
    rows = await db.execute(
        select(
            parent_fk,
            func.count(child_model.id),
            func.count(case((child_model.status != "ready", 1))),
        ).group_by(parent_fk)
    )
    return {pid: (total, unfinished) for pid, total, unfinished in rows}


async def _blocked_loops(db) -> list[str]:
    loops = await db.scalars(
        select(Loop)
        .where(Loop.announced_at.is_(None), Loop.status != "ready")
        .order_by(Loop.created_at)
    )
    return [
        f"{loop.title} — status={loop.status}, uploaded {_both_zones(loop.created_at)}"
        for loop in loops.all()
    ]


async def _blocked_children(db, parent_model, child_model, parent_fk) -> list[str]:
    """Unannounced parents that the readiness rule is holding back, and why."""
    parents = await db.scalars(
        select(parent_model)
        .where(parent_model.announced_at.is_(None))
        .order_by(parent_model.created_at)
    )
    parents = list(parents.all())
    if not parents:
        return []

    health = await _child_health(db, child_model, parent_fk)
    lines = []
    for parent in parents:
        total, unfinished = health.get(parent.id, (0, 0))
        if total and not unfinished:
            unpublished = (
                isinstance(parent, StemPack) and parent.published_at is None
            )
            if not unpublished:
                continue  # ready and queued, not blocked
            reason = "ready, but never published by the producer"
        elif total == 0:
            reason = "no files uploaded yet — a parent row with no children"
        else:
            reason = f"{unfinished} of {total} files still processing or failed"
        lines.append(
            f"{parent.title} — {reason}, created {_both_zones(parent.created_at)}"
        )
    return lines


async def _beat_heartbeat(db, now: datetime) -> tuple[list[str], bool]:
    """Whether celery beat is running at all, judged from its other jobs.

    The digest is not the only thing on the beat schedule, and the neighbours
    leave marks. cleanup_expired_downloads deletes every expired download row
    once an hour, so an expired row that is still here is one no sweep came
    for — and its age is roughly how long beat has been down.
    reconcile_store_prices stamps price_synced_at, which is positive proof beat
    fired, though only when there was pending work for it to do.

    This matters because it is answerable from the database. On a hosted
    platform there is no container to check, and "is beat alive" is otherwise
    indistinguishable from "the digest had nothing to send".

    Returns the evidence, and whether it shows beat to be down.
    """
    stale = await db.scalar(
        select(func.count(Download.id)).where(Download.expires_at < now)
    )
    oldest = await db.scalar(
        select(func.min(Download.expires_at)).where(Download.expires_at < now)
    )
    last_sync = None
    for model in (Loop, DrumKit, Drone):
        stamped = await db.scalar(select(func.max(model.price_synced_at)))
        if stamped and (last_sync is None or stamped > last_sync):
            last_sync = stamped

    lines = []
    # An hour of slack: the cleanup runs hourly, so a row that expired forty
    # minutes ago is simply waiting its turn, not evidence of anything.
    down = bool(stale and oldest and oldest < now - timedelta(hours=2))
    if stale:
        lines.append(
            f"{stale} expired download row(s) still present; oldest expired "
            f"{_both_zones(oldest)}"
        )
        if down:
            behind = now - oldest
            lines.append(
                f"cleanup_expired_downloads runs hourly and has not — beat looks "
                f"down for about {behind.days}d {behind.seconds // 3600}h"
            )
    else:
        lines.append("No expired downloads outstanding — the hourly cleanup is keeping up.")

    if last_sync:
        lines.append(f"Last store price sync: {_both_zones(last_sync)}")
    else:
        lines.append("No store price sync has ever stamped a row (inconclusive on its own).")
    return lines, down


async def _announced_since(db, since: datetime) -> list[str]:
    """Items stamped since a given moment — i.e. claimed by a sweep that ran."""
    lines = []
    for key, model in RELEASABLE.items():
        rows = await db.scalars(
            select(model)
            .where(model.announced_at.is_not(None), model.announced_at >= since)
            .order_by(model.announced_at)
        )
        for item in rows.all():
            lines.append(f"{key}:{item.id}  {item.title} — stamped {_both_zones(item.announced_at)}")
    return lines


def _section(title: str, lines: list[str], empty: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    if not lines:
        print(f"  {empty}")
        return
    for line in lines:
        print(f"  {line}")


def _delivery_problem(settings) -> str | None:
    """Why the configured backend would drop every message, if it would.

    send_email logs "email.skipped" and returns when its backend has no
    credentials, and send_bulk_email counts that as sent. So a missing key
    produces a run that claims every item and reports sent=N, failed=0, while
    delivering nothing — the one failure that looks healthy in the log.
    """
    backend = settings.email_backend
    smtp_ready = bool(settings.smtp_user and settings.smtp_password)
    resend_ready = bool(settings.resend_api_key)

    if backend == "resend" and not resend_ready:
        return "MISSING — RESEND_API_KEY is empty, every message is skipped"
    if backend == "smtp" and not smtp_ready:
        return "MISSING — SMTP_USER/SMTP_PASSWORD empty, every message is skipped"
    if backend == "fallback" and not (resend_ready or smtp_ready):
        return "MISSING — neither Resend nor SMTP is configured"
    return None


async def report() -> None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    last_run, next_run = _run_window(settings.content_digest_hour_utc, now)

    print("Digest configuration")
    print("--------------------")
    print(f"  enabled            {settings.content_digest_enabled}")
    print(f"  scheduled hour     {settings.content_digest_hour_utc:02d}:00 UTC")
    print(f"  now                {_both_zones(now)}")
    print(f"  last due run       {_both_zones(last_run)}")
    print(f"  next due run       {_both_zones(next_run)}")
    print(f"  email backend      {settings.email_backend}")
    delivery = _delivery_problem(settings)
    print(f"  credentials        {delivery or 'present for this backend'}")

    async with AsyncSessionLocal() as db:
        present = await _digest_columns(db)
        missing = [t for t in ("loops", "drones", "drum_kits", "stem_packs") if t not in present]
        _section(
            "Schema",
            [f"announced_at MISSING from {t}" for t in missing],
            "announced_at present on all four content tables (migration applied).",
        )

        digest, claim_ids = await content_digest_service.collect(db)
        queued = [
            f"{key}: {item.title}" + (f" ({item.subtitle})" if item.subtitle else "")
            for key, _label, items in digest.sections()
            for item in items
        ]
        trimmed = sum(len(v) for v in claim_ids.values()) - digest.total
        if trimmed > 0:
            queued.append(f"... and {trimmed} more beyond the per-section cap")
        _section(
            f"Queued for the next digest ({digest.total} item(s))",
            queued,
            "Nothing is waiting. The next run will send no mail at all.",
        )

        blocked = await _blocked_loops(db)
        for _key, label, parent, child, fk in CHILD_TYPES:
            blocked += [f"{label[:-1].lower()}: {line}" for line in
                        await _blocked_children(db, parent, child, fk)]
        _section(
            "Uploaded but NOT queued (not ready)",
            blocked,
            "Nothing is stuck — every unannounced item is ready to go.",
        )

        claimed = await _announced_since(db, last_run)
        _section(
            f"Claimed since the last due run ({_both_zones(last_run)})",
            claimed,
            "Nothing was stamped. Either there was nothing new, or the sweep did not run.",
        )

        beat_lines, beat_down = await _beat_heartbeat(db, now)
        _section("Is celery beat running?", beat_lines, "")

        recipients = await broadcast_service.resolve_recipients(db, BroadcastAudience.all)
        print(f"\nRecipients: {len(recipients)} address(es) would receive the next digest.")

    print("\nVerdict")
    print("-------")
    if not settings.content_digest_enabled:
        print("  CONTENT_DIGEST_ENABLED is false — the task returns before doing anything.")
    elif missing:
        print("  The migration has not run on this database. Every sweep is erroring.")
        print("  Fix: alembic upgrade head")
    elif delivery:
        print(f"  The mail backend has no credentials: {delivery}.")
        print("  A sweep still claims every item and still logs sent=N, failed=0, so the")
        print("  log looks healthy while nothing is delivered. Set the credentials, then")
        print("  release anything already claimed:")
        print("    python -m scripts.digest_status --release <type>:<id>")
    elif not recipients:
        print("  No recipients resolve. The sweep exits before sending and claims nothing.")
    elif digest.total and not claimed:
        print(f"  {digest.total} item(s) are ready and were NOT claimed at {last_run:%H:%M} UTC.")
        print("  The sweep did not run.")
        if beat_down:
            print("  The hourly download cleanup has not run either, so this is not specific")
            print("  to the digest: celery beat is not running. A worker alone is not enough")
            print("  — beat is a separate process, and nothing on the schedule fires without")
            print("  it. Start it alongside the worker:")
            print("    celery -A app.tasks.celery_app.celery_app beat --loglevel=info")
        else:
            print("  Beat's other jobs look healthy, so check the worker log for this task:")
            print("    content_digest.disabled / .nothing_new / .no_recipients / .claimed")
    elif claimed and not digest.total:
        print("  The sweep ran and claimed these items, so any missing mail failed during")
        print("  sending, after the claim. Check the worker log for content_digest.sent /")
        print("  email.bulk.chunk_failed, then re-queue with:")
        print("    python -m scripts.digest_status --release <type>:<id>")
    elif digest.total:
        print(f"  {digest.total} item(s) are queued and will go out at {_both_zones(next_run)}.")
    elif blocked:
        print("  Nothing is queued because the uploads above have not finished processing.")
    else:
        print("  Nothing is queued and nothing is stuck. Anything uploaded before the digest")
        print("  was deployed was backfilled as already announced and will not be re-sent;")
        print("  re-queue it deliberately with --release if it should go out.")


async def release(refs: list[str]) -> None:
    """Clear announced_at so an item returns to the next digest.

    The sweep stamps items before it sends, so a send that fails leaves content
    marked as announced but never delivered. This is the way back.
    """
    now_pending = 0
    async with AsyncSessionLocal() as db:
        for ref in refs:
            kind, _, raw_id = ref.partition(":")
            model = RELEASABLE.get(kind)
            if not model:
                print(f"  {ref}: unknown type — expected one of {', '.join(RELEASABLE)}")
                continue
            try:
                item_id = uuid.UUID(raw_id)
            except ValueError:
                print(f"  {ref}: not a valid uuid")
                continue
            result = await db.execute(
                update(model)
                .where(model.id == item_id, model.announced_at.is_not(None))
                .values(announced_at=None)
            )
            if result.rowcount:
                now_pending += result.rowcount
                print(f"  {ref}: released — it will be in the next digest")
            else:
                print(f"  {ref}: no change (already unannounced, or no such row)")
        await db.commit()
    print(f"\nReleased {now_pending} item(s).")


def _run(coro) -> None:
    """Run one step, then hand the engine back empty.

    Every step here — release, report, and the digest task itself — opens its
    own event loop. Pooled asyncpg connections belong to the loop that created
    them, so a connection left in the pool by one step blows up when the next
    step reuses it. Disposing between steps keeps each loop to its own.
    """
    async def _with_dispose():
        try:
            await coro
        finally:
            await engine.dispose()

    asyncio.run(_with_dispose())


def main() -> None:
    # APP_ENV=development turns on SQLAlchemy's statement echo, which buries a
    # report meant to be read by a person. Raising the logger's level does not
    # help — an echoing engine logs through a proxy that consults its own echo
    # flag, not the level — so turn the flag itself off.
    engine.echo = False

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release",
        action="append",
        default=[],
        metavar="TYPE:ID",
        help="Clear announced_at on an item so the next digest picks it up again, "
             "e.g. --release loop:0e0e....",
    )
    parser.add_argument(
        "--send-now",
        action="store_true",
        help="Run the digest immediately instead of waiting for the scheduled hour. "
             "Sends real mail to the full list; requires --yes.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm --send-now.",
    )
    args = parser.parse_args()

    if args.release:
        print("Releasing items back into the digest")
        print("-----------------------------------")
        _run(release(args.release))
        print()

    _run(report())

    if args.send_now:
        if not args.yes:
            print("\n--send-now needs --yes. It emails every recipient listed above.")
            return
        print("\nRunning the digest now...")
        # Imported here so a plain report never needs a broker connection. The
        # task body is an ordinary function; calling it runs the sweep inline in
        # this process rather than queueing it for a worker.
        from app.tasks.notification_tasks import send_content_digest

        send_content_digest()
        print("Done — see the log lines above for what was sent.")


if __name__ == "__main__":
    main()
