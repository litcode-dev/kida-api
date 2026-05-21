import asyncio
import structlog
from app.tasks.celery_app import celery_app

log = structlog.get_logger()


@celery_app.task
def send_registration_email(user_id: str):
    log.info("registration_email.task_started", user_id=user_id)

    async def _run():
        from app.database import AsyncSessionLocal
        from app.models.user import User
        from app.services.email_service import send_email, registration_html
        import uuid

        async with AsyncSessionLocal() as db:
            user = await db.get(User, uuid.UUID(user_id))
            if not user:
                log.warning("registration_email.user_not_found", user_id=user_id)
                return
            log.info("registration_email.sending", user_id=user_id, email=user.email)
            await send_email(
                to=user.email,
                subject="Welcome to LitMusic!",
                html=registration_html(user.full_name),
            )
            log.info("registration_email.done", user_id=user_id, email=user.email)

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
        from app.services.email_service import send_email, purchase_html
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
            await send_email(
                to=user.email,
                subject=f"Purchase confirmed — {product.title}",
                html=purchase_html(
                    full_name=user.full_name,
                    product_title=product.title,
                    product_type=product_type,
                    amount=f"{purchase.amount_paid:.2f}",
                ),
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
