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

The same digest also goes out as one push to every device, after the mail. Its
outcome is recorded on the run row and nowhere else: the email is what the run
succeeds or fails on, so a rejected broadcast can never release content that
thousands of inboxes have already received.

The mail half can be switched off on its own with CONTENT_DIGEST_EMAIL_ENABLED,
which is how it currently ships: the sweep, the daily slot and the claim are
unchanged, and the push becomes the whole announcement. That inverts the
sentence above for those runs only — see :func:`_push_only`, where an
undelivered push does release the content, because then nobody was told at all.
"""
from datetime import datetime, timezone

import structlog

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.digest_run import DigestPushStatus, DigestRun, DigestRunStatus
from app.schemas.broadcast import BroadcastAudience
from app.services import (
    broadcast_service, content_digest_service, digest_run_service, email_service,
    onesignal_service, unsubscribe_service,
)

log = structlog.get_logger()


def _as_json_ids(claim_ids: dict) -> dict[str, list[str]]:
    return {key: [str(i) for i in ids] for key, ids in claim_ids.items() if ids}


def _empty_audience(errors) -> bool:
    """OneSignal's way of saying "accepted, but nobody is subscribed"."""
    return any("not subscribed" in str(error).lower() for error in errors)


async def _send_push(
    settings, sections, total: int, key: str
) -> tuple[str, str | None]:
    """Announce the same digest to every device. Never raises.

    When the mail goes out, the mail is the digest and this is the copy that
    reaches the app: it runs after the send, and its outcome only ever lands on
    the run row, because a push that failed must not fail a run whose claim is
    what stops the mail that already went out from being sent again tomorrow.

    With the mail switched off it is the announcement rather than the copy, and
    :func:`_push_only` — not this function — decides what its failure costs.
    """
    if not settings.content_digest_push_enabled:
        return DigestPushStatus.disabled, "CONTENT_DIGEST_PUSH_ENABLED is false"

    problem = onesignal_service.delivery_problem(settings)
    if problem:
        log.error("content_digest.push_not_configured", run_key=key, reason=problem)
        return DigestPushStatus.not_configured, problem

    try:
        result = await onesignal_service.send_to_all(
            title=content_digest_service.headline(total),
            message=content_digest_service.push_message(sections),
            data={
                "type": "content_digest",
                "total": total,
                "content_types": [section for section, _label, _items in sections],
            },
        )
    except Exception as exc:  # noqa: BLE001 - the mail is already out
        log.error("content_digest.push_error", run_key=key, error=str(exc))
        return DigestPushStatus.failed, f"push raised {type(exc).__name__}: {exc}"

    status_code = result.get("status_code")
    body = result.get("body") or {}
    errors = body.get("errors") or []

    if status_code not in (200, 201):
        detail = f"OneSignal answered {status_code}: {str(errors or body)[:200]}"
        log.error("content_digest.push_rejected", run_key=key, detail=detail)
        return DigestPushStatus.failed, detail

    devices = body.get("recipients")
    if errors:
        detail = str(errors)[:200]
        if _empty_audience(errors):
            # Not a failure of the send: the broadcast was accepted and there
            # was nobody registered to deliver it to.
            log.info("content_digest.push_no_devices", run_key=key, detail=detail)
            return DigestPushStatus.no_devices, detail
        log.error("content_digest.push_rejected", run_key=key, detail=detail)
        return DigestPushStatus.failed, detail

    log.info("content_digest.pushed", run_key=key, devices=devices)
    return DigestPushStatus.sent, (
        f"queued for {devices} device(s)" if devices is not None else None
    )


async def _push_only(
    db, run: DigestRun, *, settings, key: str, digest, claim_ids: dict
) -> DigestRun:
    """The digest with CONTENT_DIGEST_EMAIL_ENABLED false: push, and no mail.

    The roundup email is off, so the broadcast is no longer the copy of the
    announcement — it is the announcement. Every rule the email path has about
    not burying content therefore transfers to the push here:

    * nothing is stamped until something can deliver it, so a push that is
      switched off or has no credentials is a refusal (``not_configured``)
      rather than a run that claims the day's content and announces none of it;
    * a push that fails after the claim releases the content again, the way a
      send where every message failed does — this time nobody received it, so
      there is no inbox for tomorrow's digest to repeat itself into.
    """
    total = digest.total
    sections = digest.sections()

    if not settings.content_digest_push_enabled:
        detail = (
            "CONTENT_DIGEST_EMAIL_ENABLED and CONTENT_DIGEST_PUSH_ENABLED are "
            "both false — the digest has no way to announce anything"
        )
        log.error("content_digest.no_channel", run_key=key, items=total)
        return await digest_run_service.finish(
            db, run, DigestRunStatus.not_configured, items=total, detail=detail,
            push_status=DigestPushStatus.disabled, push_detail=detail,
        )

    problem = onesignal_service.delivery_problem(settings)
    if problem:
        log.error(
            "content_digest.push_not_configured",
            run_key=key, items=total, reason=problem,
        )
        return await digest_run_service.finish(
            db, run, DigestRunStatus.not_configured, items=total, detail=problem,
            push_status=DigestPushStatus.not_configured, push_detail=problem,
        )

    json_ids = _as_json_ids(claim_ids)
    claimed = await content_digest_service.claim(db, claim_ids)
    run.claimed_ids = json_ids
    db.add(run)
    await db.commit()
    log.info(
        "content_digest.claimed",
        run_key=key, items=claimed, recipients=0, channel="push", ids=json_ids,
    )

    push_status, push_detail = await _send_push(settings, sections, total, key)

    if push_status != DigestPushStatus.sent:
        # Nobody was told, by any channel: hand the content back to the next
        # digest rather than leaving it stamped and never mentioned again.
        released = await content_digest_service.release(db, claim_ids)
        # An accepted broadcast with nobody subscribed is not a broken send,
        # so it is recorded the way an empty mailing list is.
        status = (
            DigestRunStatus.no_recipients
            if push_status == DigestPushStatus.no_devices
            else DigestRunStatus.failed
        )
        log.error(
            "content_digest.push_only_undelivered",
            run_key=key, items=total, push=push_status, released=released,
        )
        return await digest_run_service.finish(
            db, run, status, items=total, claimed_ids=json_ids,
            detail=(
                f"push did not deliver ({push_status}) and the digest email is "
                "off — content released for the next digest"
            ),
            push_status=push_status, push_detail=push_detail,
        )

    log.info("content_digest.push_only_sent", run_key=key, items=total)
    return await digest_run_service.finish(
        db, run, DigestRunStatus.push_only, items=total, claimed_ids=json_ids,
        detail="CONTENT_DIGEST_EMAIL_ENABLED is false — announced by push only",
        push_status=push_status, push_detail=push_detail,
    )


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

        # The mail can be switched off on its own, leaving the push to carry
        # the digest by itself. Everything above this point is shared — the
        # slot, the lock, the sweep — because a push-only digest is still one
        # digest a day announcing each item exactly once.
        if not settings.content_digest_email_enabled:
            log.info(
                "content_digest.email_disabled", run_key=key, items=digest.total
            )
            return await _push_only(
                db, run, settings=settings, key=key,
                digest=digest, claim_ids=claim_ids,
            )

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

        subject = content_digest_service.headline(total)

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

        # The same announcement, to every device. After the mail rather than
        # before it: the push is the copy, and nothing about it may change what
        # the run has already claimed and delivered.
        push_status, push_detail = await _send_push(settings, sections, total, key)

        log.info(
            "content_digest.sent",
            run_key=key, items=total, sent=sent, failed=failed, push=push_status,
        )
        return await digest_run_service.finish(
            db, run, DigestRunStatus.sent, items=total, recipients=len(recipients),
            sent=sent, failed=failed, claimed_ids=json_ids,
            push_status=push_status, push_detail=push_detail,
        )
