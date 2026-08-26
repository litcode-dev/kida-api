import asyncio
import structlog
from app.tasks.celery_app import celery_app

log = structlog.get_logger()

# The statuses a requester is emailed about. Moving a request back to "new"
# is an internal correction and sends nothing.
NOTIFIED_LOOP_REQUEST_STATUSES = ("in_progress", "fulfilled", "declined")


@celery_app.task
def send_registration_email(user_id: str):
    log.info("registration_email.task_started", user_id=user_id)

    async def _run():
        from app.database import AsyncSessionLocal
        from app.models.user import User
        from app.services.email_service import send_email, registration_html, registration_text
        import uuid

        async with AsyncSessionLocal() as db:
            user = await db.get(User, uuid.UUID(user_id))
            if not user:
                log.warning("registration_email.user_not_found", user_id=user_id)
                return
            log.info("registration_email.sending", user_id=user_id, email=user.email)
            await send_email(
                to=user.email,
                subject="Welcome to Kida",
                html=registration_html(user.full_name),
                text=registration_text(user.full_name),
            )
            log.info("registration_email.done", user_id=user_id, email=user.email)

    asyncio.run(_run())


@celery_app.task
def send_verification_email(user_id: str, code: str, ttl_minutes: int):
    log.info("verification_email.task_started", user_id=user_id)

    async def _run():
        from app.database import AsyncSessionLocal
        from app.models.user import User
        from app.services.email_service import send_email, verification_html, verification_text
        import uuid

        async with AsyncSessionLocal() as db:
            user = await db.get(User, uuid.UUID(user_id))
            if not user:
                log.warning("verification_email.user_not_found", user_id=user_id)
                return
            log.info("verification_email.sending", user_id=user_id, email=user.email)
            await send_email(
                to=user.email,
                subject="Verify your Kida email",
                html=verification_html(user.full_name, code, ttl_minutes),
                text=verification_text(user.full_name, code, ttl_minutes),
            )
            log.info("verification_email.done", user_id=user_id, email=user.email)

    asyncio.run(_run())


@celery_app.task
def send_new_user_admin_notification(user_id: str):
    """Tell the team inbox that a new account was created.

    Dispatched from the same two points as the user's welcome email: after email
    verification for password signups, and on first-time OAuth account creation.
    Silently skipped when ADMIN_NOTIFICATION_EMAIL is blank.
    """
    log.info("new_user_admin_email.task_started", user_id=user_id)

    async def _run():
        from app.config import get_settings
        from app.database import AsyncSessionLocal
        from app.models.user import User
        from app.services.email_service import (
            send_email, new_user_admin_html, new_user_admin_text,
        )
        import uuid

        recipient = get_settings().admin_notification_email
        if not recipient:
            log.info("new_user_admin_email.skipped", reason="no recipient configured")
            return

        async with AsyncSessionLocal() as db:
            user = await db.get(User, uuid.UUID(user_id))
            if not user:
                log.warning("new_user_admin_email.user_not_found", user_id=user_id)
                return

            provider = user.oauth_provider or "email"
            fields = dict(
                full_name=user.full_name,
                email=user.email,
                provider=provider,
                user_id=str(user.id),
                signed_up_at=user.created_at,
            )
            log.info("new_user_admin_email.sending", user_id=user_id, to=recipient)
            await send_email(
                to=recipient,
                subject=f"New Kida signup — {user.email}",
                html=new_user_admin_html(**fields),
                text=new_user_admin_text(**fields),
            )
            log.info("new_user_admin_email.done", user_id=user_id, to=recipient)

    asyncio.run(_run())


@celery_app.task
def send_account_deleted_admin_notification(
    user_id: str,
    full_name: str,
    email: str,
    provider: str,
    joined_at: str | None,
    actor: str = "user",
):
    """Tell the team inbox that an account was deleted.

    Unlike the signup notification, nothing can be looked up here — the user row
    is gone by the time this runs, so the caller passes every detail through.
    ``joined_at`` is an ISO-8601 string because Celery serializes args as JSON.
    ``actor`` says whether the user or an admin removed the account. It defaults
    to "user" so tasks queued by an older worker before this argument existed
    still run. Silently skipped when ADMIN_NOTIFICATION_EMAIL is blank.
    """
    log.info("account_deleted_admin_email.task_started", user_id=user_id)

    async def _run():
        from datetime import datetime, timezone
        from app.config import get_settings
        from app.services.email_service import (
            send_email, account_deleted_admin_html, account_deleted_admin_text,
        )

        recipient = get_settings().admin_notification_email
        if not recipient:
            log.info("account_deleted_admin_email.skipped", reason="no recipient configured")
            return

        fields = dict(
            full_name=full_name,
            email=email,
            provider=provider,
            user_id=user_id,
            joined_at=datetime.fromisoformat(joined_at) if joined_at else None,
            deleted_at=datetime.now(timezone.utc),
            actor=actor,
        )
        log.info("account_deleted_admin_email.sending", user_id=user_id, to=recipient, actor=actor)
        await send_email(
            to=recipient,
            subject=f"Kida account deleted — {email}",
            html=account_deleted_admin_html(**fields),
            text=account_deleted_admin_text(**fields),
        )
        log.info("account_deleted_admin_email.done", user_id=user_id, to=recipient)

    asyncio.run(_run())


@celery_app.task
def send_loop_request_admin_notification(loop_request_id: str):
    """Tell the team inbox that a user asked for a loop or a set of stems.

    Dispatched after the request row is committed, so everything the email needs
    is looked up here rather than passed through. Silently skipped when
    ADMIN_NOTIFICATION_EMAIL is blank.
    """
    log.info("loop_request_admin_email.task_started", loop_request_id=loop_request_id)

    async def _run():
        from app.config import get_settings
        from app.database import AsyncSessionLocal
        from app.models.loop_request import LoopRequest
        from app.models.user import User
        from app.services.email_service import (
            send_email, loop_request_admin_html, loop_request_admin_text,
        )
        import uuid

        recipient = get_settings().admin_notification_email
        if not recipient:
            log.info("loop_request_admin_email.skipped", reason="no recipient configured")
            return

        async with AsyncSessionLocal() as db:
            loop_request = await db.get(LoopRequest, uuid.UUID(loop_request_id))
            if not loop_request:
                log.warning(
                    "loop_request_admin_email.request_not_found",
                    loop_request_id=loop_request_id,
                )
                return

            user = await db.get(User, loop_request.user_id)
            fields = dict(
                request_type=loop_request.request_type,
                artist_name=loop_request.artist_name,
                song_title=loop_request.song_title,
                reference_link=loop_request.reference_link,
                notes=loop_request.notes,
                requester_name=(user.full_name if user else "A deleted user"),
                requester_email=(user.email if user else "unknown"),
                request_id=str(loop_request.id),
                requested_at=loop_request.created_at,
            )
            label = "Stems" if loop_request.request_type == "stems" else "Loop"
            log.info(
                "loop_request_admin_email.sending",
                loop_request_id=loop_request_id, to=recipient,
            )
            await send_email(
                to=recipient,
                subject=(
                    f"{label} request — {loop_request.song_title} "
                    f"by {loop_request.artist_name}"
                ),
                html=loop_request_admin_html(**fields),
                text=loop_request_admin_text(**fields),
            )
            log.info(
                "loop_request_admin_email.done",
                loop_request_id=loop_request_id, to=recipient,
            )

    asyncio.run(_run())


@celery_app.task
def send_loop_request_status_email(loop_request_id: str):
    """Tell the requester their loop or stems request moved.

    Only the three statuses a person is waiting on send mail — moving a request
    back to "new" is an internal correction, so the task exits quietly rather
    than emailing about it. Dispatched after the status is committed, so what is
    read here is what was saved.
    """
    log.info("loop_request_status_email.task_started", loop_request_id=loop_request_id)

    async def _run():
        from app.database import AsyncSessionLocal
        from app.models.loop_request import LoopRequest
        from app.models.user import User
        from app.services.email_service import (
            send_email,
            loop_request_status_html,
            loop_request_status_subject,
            loop_request_status_text,
        )
        import uuid

        async with AsyncSessionLocal() as db:
            loop_request = await db.get(LoopRequest, uuid.UUID(loop_request_id))
            if not loop_request:
                log.warning(
                    "loop_request_status_email.request_not_found",
                    loop_request_id=loop_request_id,
                )
                return

            if loop_request.status not in NOTIFIED_LOOP_REQUEST_STATUSES:
                log.info(
                    "loop_request_status_email.skipped",
                    loop_request_id=loop_request_id,
                    reason=f"status is {loop_request.status}",
                )
                return

            user = await db.get(User, loop_request.user_id)
            if not user:
                log.warning(
                    "loop_request_status_email.user_not_found",
                    loop_request_id=loop_request_id,
                )
                return

            fields = dict(
                full_name=user.full_name,
                status=loop_request.status,
                request_type=loop_request.request_type,
                artist_name=loop_request.artist_name,
                song_title=loop_request.song_title,
            )
            log.info(
                "loop_request_status_email.sending",
                loop_request_id=loop_request_id,
                status=loop_request.status,
                email=user.email,
            )
            await send_email(
                to=user.email,
                subject=loop_request_status_subject(
                    loop_request.status, loop_request.request_type
                ),
                html=loop_request_status_html(**fields),
                text=loop_request_status_text(**fields),
            )
            log.info(
                "loop_request_status_email.done",
                loop_request_id=loop_request_id,
                email=user.email,
            )

    asyncio.run(_run())


@celery_app.task
def send_purchase_confirmation(user_id: str, purchase_id: str):
    async def _run():
        from app.database import AsyncSessionLocal
        from app.models.user import User
        from app.models.purchase import Purchase
        from app.models.loop import Loop
        from app.models.stem_pack import StemPack
        from app.services.onesignal_service import send_purchase_confirmation_notification
        from app.services.email_service import send_email, purchase_html, purchase_text
        import uuid

        async with AsyncSessionLocal() as db:
            user = await db.get(User, uuid.UUID(user_id))
            purchase = await db.get(Purchase, uuid.UUID(purchase_id))
            if not user or not purchase:
                return

            if purchase.loop_id:
                product = await db.get(Loop, purchase.loop_id)
                product_type = "Loop"
            else:
                product = await db.get(StemPack, purchase.stem_pack_id)
                product_type = "Stem Pack"

            if not product:
                return

            # Push notification (OneSignal)
            if user.onesignal_player_id:
                await send_purchase_confirmation_notification(
                    user.onesignal_player_id, product.title
                )

            # Email
            _amount = f"{purchase.amount_paid:.2f}"
            await send_email(
                to=user.email,
                subject=f"Purchase confirmed — {product.title}",
                html=purchase_html(
                    full_name=user.full_name,
                    product_title=product.title,
                    product_type=product_type,
                    amount=_amount,
                ),
                text=purchase_text(
                    full_name=user.full_name,
                    product_title=product.title,
                    product_type=product_type,
                    amount=_amount,
                ),
            )

    asyncio.run(_run())


@celery_app.task
def send_content_digest(trigger: str = "beat", force: bool = False):
    """One email listing everything that went live since the last run.

    The work itself lives in ``digest_sender.run_digest``, because beat is not
    the only thing that starts it: the API runs the same function on a catch-up
    schedule, and an admin can trigger it by hand. Whichever gets there first
    claims the slot; the others find it taken and do nothing.

    ``trigger`` is recorded on the run row, so "is beat actually firing?" is a
    question the digest history can answer.
    """
    async def _run():
        from app.services.digest_sender import run_digest

        await run_digest(trigger=trigger, force=force)

    asyncio.run(_run())


@celery_app.task
def send_new_content_push(title: str, content_type: str, item_id: str):
    """Broadcast a push notification to all devices when new content goes live."""
    async def _run():
        from app.services.onesignal_service import send_to_all

        type_labels = {
            "loop": "New Loop",
            "drum_kit": "New Drum Kit",
            "drone_pad": "New Drone Pad",
            "stem_pack": "New Stem Pack",
        }
        push_title = type_labels.get(content_type, "New Content on Kida")
        await send_to_all(
            title=push_title,
            message=f'"{title}" just dropped on Kida.',
            data={"type": f"new_{content_type}", f"{content_type}_id": item_id},
        )

    asyncio.run(_run())


@celery_app.task
def send_new_loop_notification(loop_id: str):
    async def _run():
        from app.database import AsyncSessionLocal
        from app.models.loop import Loop
        from app.models.user import User
        from app.services.onesignal_service import send_new_loop_notification as _notify
        from sqlalchemy import select
        import uuid

        async with AsyncSessionLocal() as db:
            loop = await db.get(Loop, uuid.UUID(loop_id))
            if not loop:
                return
            users = await db.scalars(
                select(User).where(User.onesignal_player_id.is_not(None))
            )
            for user in users.all():
                await _notify(
                    user.onesignal_player_id,
                    loop.genre.value,
                    loop.title,
                    loop_id,
                )

    asyncio.run(_run())


@celery_app.task
def send_app_download_email(request_id: str):
    """Email the desktop-app download link for a previously created request."""
    log.info("app_download_email.task_started", request_id=request_id)

    async def _run():
        from app.database import AsyncSessionLocal
        from app.models.app_download_request import AppDownloadRequest
        from app.services import app_download_service
        from app.services.email_service import send_email, app_download_html, app_download_text
        import uuid

        async with AsyncSessionLocal() as db:
            req = await db.get(AppDownloadRequest, uuid.UUID(request_id))
            if not req:
                log.warning("app_download_email.request_not_found", request_id=request_id)
                return

            link = app_download_service.build_download_link(req.token)
            os_label = app_download_service.OS_LABELS.get(req.os, req.os)
            log.info("app_download_email.sending", request_id=request_id, email=req.email)
            await send_email(
                to=req.email,
                subject=f"Your Kida download link for {os_label}",
                html=app_download_html(os_label, link, req.expires_at),
                text=app_download_text(os_label, link, req.expires_at),
            )
            log.info("app_download_email.done", request_id=request_id, email=req.email)

    asyncio.run(_run())


@celery_app.task
def send_broadcast_email(
    subject: str,
    body: str,
    audience: str,
    heading: str | None = None,
    cta_label: str | None = None,
    cta_url: str | None = None,
):
    """Send an admin-authored announcement to a resolved audience.

    Recipients are resolved inside the task rather than passed in, so a large
    audience does not have to travel through the broker and the list reflects
    the moment of sending.

    send_email swallows and logs per-address failures, so one bad address cannot
    abort the run; the summary log line reports how many were attempted.
    """
    log.info("broadcast_email.task_started", audience=audience, subject=subject)

    async def _run():
        from app.database import AsyncSessionLocal
        from app.schemas.broadcast import BroadcastAudience
        from app.services import broadcast_service
        from app.services.email_service import send_email, broadcast_html, broadcast_text

        async with AsyncSessionLocal() as db:
            recipients = await broadcast_service.resolve_recipients(
                db, BroadcastAudience(audience)
            )

        if not recipients:
            log.info("broadcast_email.no_recipients", audience=audience)
            return

        html = broadcast_html(subject, body, heading, cta_label, cta_url)
        text = broadcast_text(subject, body, heading, cta_label, cta_url)

        for email in recipients:
            await send_email(to=email, subject=subject, html=html, text=text)

        log.info(
            "broadcast_email.done",
            audience=audience,
            subject=subject,
            recipients=len(recipients),
        )

    asyncio.run(_run())


