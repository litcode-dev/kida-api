"""Recording a deletion, and finishing it off at the third parties.

Deleting the rows we hold is only half the promise. The app hands personal data
to RevenueCat (subscriber records) and OneSignal (push subscriptions), and a
deletion that stops at our database leaves the person's data sitting in two
other companies' systems.

The rules those calls follow:

* **Best effort.** A third-party outage must never roll back or block the local
  delete. The local delete is committed first and is never undone by anything
  here.
* **Idempotent.** A 404 is success. Re-running a completed propagation is a
  no-op, so the retry sweep can be as aggressive as it likes.
* **Retryable.** Every outstanding call is a row in
  ``account_deletion_propagation`` with a backoff, not a log line somebody has
  to notice. Rows only leave the table when the work is done or abandoned.

Timing: the audit row and its propagation work are written in the same
transaction as the delete, and the third-party calls are queued the moment that
transaction commits. There is no pending state to wait through — a deletion is
carried out in one go.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.deletion_audit import (
    AccountDeletionAudit,
    AccountDeletionPropagation,
    DeletionActor,
    PropagationStatus,
)

log = structlog.get_logger()

# Mixed into the subject hash so it can never collide with, or be replayed as,
# another signature made with the same SECRET_KEY. Same discipline as
# unsubscribe_service._TOKEN_PURPOSE.
_SUBJECT_PURPOSE = b"kida:deletion-audit:v1"

# Backoff between propagation attempts, capped so a long outage still gets a
# retry every few hours rather than drifting into next week.
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 6 * 3600


def subject_hash(user_id) -> str:
    """Stable, non-reversible handle for a deleted account.

    Recomputable from a user id, so an enquiry carrying one can be answered, but
    the audit table on its own is not a list of who ever held an account.
    """
    settings = get_settings()
    message = _SUBJECT_PURPOSE + b"|" + str(user_id).encode()
    return hmac.new(settings.secret_key.encode(), message, hashlib.sha256).hexdigest()


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(_BACKOFF_BASE_SECONDS * (2 ** attempts), _BACKOFF_CAP_SECONDS))


def _max_attempts() -> int:
    return get_settings().deletion_propagation_max_attempts


async def record_deletion(
    db: AsyncSession,
    *,
    user_id,
    actor: DeletionActor,
    requested_at: datetime | None,
    revenuecat_app_user_ids: list[str],
    onesignal_subscription_id: str | None,
    onesignal_external_id: str | None,
) -> uuid.UUID:
    """Write the audit row (and its propagation work) for an account being purged.

    Must be called while the user's rows still exist — it reads the third-party
    identifiers off them — but within the same transaction as the delete, so a
    rolled-back delete leaves no audit claiming otherwise.

    Returns the audit id.
    """
    ids = [i for i in dict.fromkeys(revenuecat_app_user_ids) if i]

    audit = AccountDeletionAudit(
        id=uuid.uuid4(),
        subject_hash=subject_hash(user_id),
        actor=actor,
        requested_at=requested_at,
        purged_at=datetime.now(timezone.utc),
        revenuecat_status=PropagationStatus.pending if ids else PropagationStatus.skipped,
        onesignal_status=(
            PropagationStatus.pending
            if (onesignal_subscription_id or onesignal_external_id)
            else PropagationStatus.skipped
        ),
    )
    db.add(audit)

    if audit.revenuecat_status is PropagationStatus.pending or (
        audit.onesignal_status is PropagationStatus.pending
    ):
        db.add(
            AccountDeletionPropagation(
                id=uuid.uuid4(),
                audit_id=audit.id,
                revenuecat_app_user_ids="\n".join(ids) or None,
                onesignal_subscription_id=onesignal_subscription_id,
                onesignal_external_id=onesignal_external_id,
                next_attempt_at=datetime.now(timezone.utc),
            )
        )
    return audit.id


async def _propagate_revenuecat(ids: list[str]) -> tuple[PropagationStatus, str | None]:
    """Delete the subscriber under every id RevenueCat might know them by."""
    from app.exceptions import AppError
    from app.services.revenuecat_service import RevenueCatClient

    if not ids:
        return PropagationStatus.skipped, None
    if not get_settings().revenuecat_api_key:
        return PropagationStatus.skipped, "RevenueCat not configured"

    client = RevenueCatClient()
    transient = False
    permanent: list[str] = []
    for app_user_id in ids:
        try:
            if not await client.delete_subscriber(app_user_id):
                transient = True
        except AppError as exc:
            # A permanent 4xx for this id. The ids fail independently — one
            # being malformed says nothing about the others — so record it and
            # carry on rather than abandoning the whole account.
            permanent.append(exc.message)

    if transient:
        # Something might still succeed on a retry, so stay queued. The ids that
        # already succeeded will 404 next time, which counts as done.
        return PropagationStatus.pending, "RevenueCat unavailable"
    if permanent and len(permanent) == len(ids):
        # Every id was rejected outright. Nothing to retry.
        return PropagationStatus.failed, "; ".join(permanent)[:400]
    return PropagationStatus.done, None


async def _propagate_onesignal(
    external_id: str | None, subscription_id: str | None
) -> tuple[PropagationStatus, str | None]:
    """Remove the person from OneSignal.

    Prefers the user-level delete (every device); falls back to the single
    subscription we have on file when no external_id alias was ever set.
    """
    from app.services import onesignal_service

    if not external_id and not subscription_id:
        return PropagationStatus.skipped, None
    settings = get_settings()
    if not settings.onesignal_api_key or not settings.onesignal_app_id:
        return PropagationStatus.skipped, "OneSignal not configured"

    transient = False
    try:
        if external_id:
            if not await onesignal_service.delete_user_by_external_id(external_id):
                transient = True
        if subscription_id:
            if not await onesignal_service.delete_subscription(subscription_id):
                transient = True
    except RuntimeError as exc:
        return PropagationStatus.failed, str(exc)[:400]

    if transient:
        return PropagationStatus.pending, "OneSignal unavailable"
    return PropagationStatus.done, None


async def propagate_one(db: AsyncSession, row: AccountDeletionPropagation) -> bool:
    """Attempt both third-party deletions for one outstanding row.

    Returns True when the row is finished with (done, abandoned, or nothing left
    to do) and has been removed; False when it stays queued for another attempt.
    Never raises — a failure here must not disturb anything else.
    """
    audit = await db.get(AccountDeletionAudit, row.audit_id)
    if audit is None:
        # Orphaned work with nothing to record against. Drop it.
        await db.delete(row)
        await db.commit()
        return True

    ids = [i for i in (row.revenuecat_app_user_ids or "").split("\n") if i]
    errors: list[str] = []

    if audit.revenuecat_status is PropagationStatus.pending:
        status, err = await _propagate_revenuecat(ids)
        audit.revenuecat_status = status
        if err:
            errors.append(err)

    if audit.onesignal_status is PropagationStatus.pending:
        status, err = await _propagate_onesignal(
            row.onesignal_external_id, row.onesignal_subscription_id
        )
        audit.onesignal_status = status
        if err:
            errors.append(err)

    row.attempts += 1
    audit.last_error = "; ".join(errors)[:500] or None

    outstanding = PropagationStatus.pending in (
        audit.revenuecat_status, audit.onesignal_status
    )

    if not outstanding:
        # Nothing left to send. The identifiers have done their job and must not
        # outlive it — this is what keeps the audit trail free of personal data.
        await db.delete(row)
        await db.commit()
        log.info(
            "account_deletion.propagated",
            audit_id=str(audit.id),
            revenuecat=audit.revenuecat_status.value,
            onesignal=audit.onesignal_status.value,
            attempts=row.attempts,
        )
        return True

    if row.attempts >= _max_attempts():
        # Give up sending, but keep the audit honest about it and stop holding
        # the identifiers. A "failed" row is the thing to alert on.
        for field in ("revenuecat_status", "onesignal_status"):
            if getattr(audit, field) is PropagationStatus.pending:
                setattr(audit, field, PropagationStatus.failed)
        await db.delete(row)
        await db.commit()
        log.error(
            "account_deletion.propagation_abandoned",
            audit_id=str(audit.id), attempts=row.attempts, error=audit.last_error,
        )
        return True

    row.next_attempt_at = datetime.now(timezone.utc) + _backoff(row.attempts)
    await db.commit()
    log.warning(
        "account_deletion.propagation_retry",
        audit_id=str(audit.id),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at.isoformat(),
        error=audit.last_error,
    )
    return False


async def run_pending(db: AsyncSession, limit: int = 50) -> dict[str, int]:
    """Work through every propagation row whose backoff has elapsed.

    Each row is handled independently so one stuck account cannot hold up the
    rest. Safe to call as often as you like.
    """
    now = datetime.now(timezone.utc)
    rows = (
        await db.scalars(
            select(AccountDeletionPropagation)
            .where(AccountDeletionPropagation.next_attempt_at <= now)
            .order_by(AccountDeletionPropagation.next_attempt_at)
            .limit(limit)
        )
    ).all()

    finished = 0
    requeued = 0
    for row in rows:
        try:
            if await propagate_one(db, row):
                finished += 1
            else:
                requeued += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sweep
            await db.rollback()
            requeued += 1
            log.error(
                "account_deletion.propagation_error",
                propagation_id=str(row.id), error=str(exc),
            )
    return {"finished": finished, "requeued": requeued, "considered": len(rows)}
