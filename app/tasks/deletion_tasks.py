"""Finishing an account deletion at the third parties.

Two entry points, deliberately overlapping:

* :func:`propagate_account_deletion` runs right after a deletion, so in the
  normal case RevenueCat and OneSignal hear about it within seconds.
* :func:`retry_pending_deletion_propagation` sweeps on the beat schedule and
  picks up anything the first path missed — a third-party outage, a worker that
  died mid-task, or an enqueue that never reached the broker.

The sweep is the guarantee. The immediate call is only an optimisation, which
is why neither the delete nor the sweep treats a failure of the other as fatal.
"""
import asyncio

import structlog

from app.tasks.celery_app import celery_app

log = structlog.get_logger()


@celery_app.task
def propagate_account_deletion(audit_id: str):
    """Send one deleted account on to RevenueCat and OneSignal.

    Takes the audit id rather than any identifier, so the queued message itself
    carries no personal data — a broker with its own retention does not become
    another copy of the address we just deleted.

    Never raises: a failure leaves the propagation row queued with a backoff and
    the beat sweep retries it.
    """
    log.info("account_deletion.propagate.task_started", audit_id=audit_id)

    async def _run():
        import uuid as _uuid

        from sqlalchemy import select

        from app.database import AsyncSessionLocal
        from app.models.deletion_audit import AccountDeletionPropagation
        from app.services import account_deletion_service

        async with AsyncSessionLocal() as db:
            row = (
                await db.scalars(
                    select(AccountDeletionPropagation).where(
                        AccountDeletionPropagation.audit_id == _uuid.UUID(audit_id)
                    )
                )
            ).first()
            if row is None:
                # Nothing outstanding — already propagated, or there was never
                # anything to send. Both are fine.
                log.info("account_deletion.propagate.nothing_queued", audit_id=audit_id)
                return
            await account_deletion_service.propagate_one(db, row)

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - the sweep retries; never fail the task
        log.error(
            "account_deletion.propagate.task_failed", audit_id=audit_id, error=str(exc)
        )


@celery_app.task
def send_deletion_request_email(user_id: str):
    """Mail the confirmation link for a deletion asked for from the public page.

    Takes a user id, not an address, so the queue does not become another store
    of who asked to be deleted. The token is minted here, which also starts its
    short expiry from the moment the mail actually goes out rather than from
    when the request was accepted.
    """
    log.info("deletion_request_email.task_started", user_id=user_id)

    async def _run():
        import uuid as _uuid

        from app.database import AsyncSessionLocal
        from app.models.user import User
        from app.config import get_settings
        from app.services import deletion_request_service
        from app.services.email_service import (
            send_email, deletion_request_html, deletion_request_text,
        )

        async with AsyncSessionLocal() as db:
            user = await db.get(User, _uuid.UUID(user_id))
            if user is None:
                log.info("deletion_request_email.user_gone", user_id=user_id)
                return
            address = user.email

        ttl = get_settings().deletion_request_token_ttl_minutes
        token = deletion_request_service.make_token(address)
        url = deletion_request_service.confirmation_url(token)
        await send_email(
            to=address,
            subject="Confirm deleting your Kida account",
            html=deletion_request_html(url, ttl),
            text=deletion_request_text(url, ttl),
        )
        log.info("deletion_request_email.sent", user_id=user_id)

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - the person can just ask again
        log.error("deletion_request_email.failed", user_id=user_id, error=str(exc))


@celery_app.task
def retry_pending_deletion_propagation():
    """Retry every third-party deletion still outstanding past its backoff."""

    async def _run():
        from app.database import AsyncSessionLocal
        from app.services import account_deletion_service

        async with AsyncSessionLocal() as db:
            return await account_deletion_service.run_pending(db)

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - next tick tries again
        log.error("account_deletion.sweep_failed", error=str(exc))
        return

    if result["considered"]:
        log.info("account_deletion.sweep_done", **result)
