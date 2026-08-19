"""Running the daily digest: one implementation, several ways to start it.

Beat used to be the only thing that could fire the digest, and the task left no
record of itself, so "no mail arrived" and "nothing ran" looked the same. Both
are fixed here:

* the work lives in :func:`run_digest`, which celery beat, the API's in-process
  scheduler, the admin endpoint and the diagnostic script all call — a digest
  goes out even on a deployment where no beat process was ever started;
* every attempt writes a ``digest_runs`` row, and that row is also the lock, so
  the extra entry points cannot produce a second copy of the same digest.

Two failure modes the old task hid are now refusals rather than false successes:
a mail backend with no credentials (which logged sent=N while delivering
nothing), and a send where every message failed (which left the content stamped
as announced and therefore never mentioned again).
"""
from datetime import datetime, timezone

import structlog

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.digest_run import DigestRun, DigestRunStatus
from app.schemas.broadcast import BroadcastAudience
from app.services import (
    broadcast_service, content_digest_service, digest_run_service, email_service,
    unsubscribe_service,
)

log = structlog.get_logger()


def _as_json_ids(claim_ids: dict) -> dict[str, list[str]]:
    return {key: [str(i) for i in ids] for key, ids in claim_ids.items() if ids}


async def run_digest(
    *, trigger: str, force: bool = False, now: datetime | None = None
) -> DigestRun | None:
    """Collect, claim and send one digest. Returns the run row, or None.

    None means this process did not run the digest at all: either the feature is
    switched off, or another process already owns the slot. ``force`` runs
    outside the daily slot (the admin "send now" button) and always gets its own
    key, so it can never cost the day its scheduled digest.
    """
    settings = get_settings()
    now = now or datetime.now(timezone.utc)

    if not settings.content_digest_enabled:
        log.info("content_digest.disabled")
        return None

    if force:
        key, scheduled_for = digest_run_service.manual_key(now), now
    else:
        slot = digest_run_service.due_slot(settings.content_digest_hour_utc, now)
        key, scheduled_for = digest_run_service.slot_key(slot), slot

    async with AsyncSessionLocal() as db:
        run = await digest_run_service.claim(
            db, key=key, scheduled_for=scheduled_for, trigger=trigger
        )
        if run is None:
            log.info("content_digest.already_claimed", run_key=key, trigger=trigger)
            return None

        log.info("content_digest.started", run_key=key, trigger=trigger)

        digest, claim_ids = await content_digest_service.collect(db)
        if digest.is_empty():
            log.info("content_digest.nothing_new", run_key=key)
            return await digest_run_service.finish(db, run, DigestRunStatus.empty)

        # Before anything is stamped: a backend with no credentials would skip
        # every message while the send still reported success, and the content
        # would be marked announced and never mentioned again.
        problem = email_service.delivery_problem(settings)
        if problem:
            log.error(
                "content_digest.not_configured",
                run_key=key, items=digest.total, reason=problem,
            )
            return await digest_run_service.finish(
                db, run, DigestRunStatus.not_configured,
                items=digest.total, detail=problem,
            )

        # Everyone who gets marketing mail: users and newsletter subscribers,
        # minus anyone who unsubscribed.
        recipients = await broadcast_service.resolve_recipients(
            db, BroadcastAudience.all
        )
        if not recipients:
            log.info("content_digest.no_recipients", run_key=key, items=digest.total)
            return await digest_run_service.finish(
                db, run, DigestRunStatus.no_recipients, items=digest.total,
                detail="no addresses resolved for the 'all' audience",
            )

        sections = digest.sections()
        total = digest.total
        json_ids = _as_json_ids(claim_ids)

        # Claimed before sending: a crashed send must not re-blast the same
        # digest to the whole list on the next run. The ids are kept on the run
        # row as well as in the log, so a lost digest can be released by id.
        claimed = await content_digest_service.claim(db, claim_ids)
        run.claimed_ids = json_ids
        db.add(run)
        await db.commit()
        log.info(
            "content_digest.claimed",
            run_key=key, items=claimed, recipients=len(recipients), ids=json_ids,
        )

        subject = "1 new drop on Kida" if total == 1 else f"{total} new drops on Kida"

        # Rendered per address: the unsubscribe link and its List-Unsubscribe
        # headers are signed for one recipient, so one shared body would let
        # anybody unsubscribe everybody.
        def _build(address: str):
            url = unsubscribe_service.unsubscribe_url(address)
            return (
                email_service.content_digest_html(sections, total, url),
                email_service.content_digest_text(sections, total, url),
                unsubscribe_service.list_unsubscribe_headers(address),
            )

        try:
            sent, failed = await email_service.send_bulk_email(
                recipients=recipients, subject=subject, build=_build
            )
        except Exception as exc:  # noqa: BLE001 - the run row must record this
            # Chunk failures are handled inside send_bulk_email, so an exception
            # here broke the loop partway: some addresses may already have the
            # mail. The claim stays (re-blasting the whole list is the more
            # expensive mistake) and the ids on the row are the way back.
            log.error(
                "content_digest.send_error",
                run_key=key, items=total, error=str(exc), ids=json_ids,
            )
            return await digest_run_service.finish(
                db, run, DigestRunStatus.failed, items=total,
                recipients=len(recipients), failed=len(recipients),
                claimed_ids=json_ids, detail=f"send raised {type(exc).__name__}: {exc}",
            )

        if sent == 0:
            # Every message was rejected, so nothing was announced to anyone.
            # Unstamp the content: it goes out with the next digest instead of
            # disappearing silently, which is the failure this whole run row
            # exists to make visible.
            released = await content_digest_service.release(db, claim_ids)
            log.error(
                "content_digest.all_failed",
                run_key=key, items=total, recipients=len(recipients),
                released=released,
            )
            return await digest_run_service.finish(
                db, run, DigestRunStatus.failed, items=total,
                recipients=len(recipients), failed=failed, claimed_ids=json_ids,
                detail="every message failed — content released for the next digest",
            )

        log.info(
            "content_digest.sent",
            run_key=key, items=total, sent=sent, failed=failed,
        )
        return await digest_run_service.finish(
            db, run, DigestRunStatus.sent, items=total, recipients=len(recipients),
            sent=sent, failed=failed, claimed_ids=json_ids,
        )
