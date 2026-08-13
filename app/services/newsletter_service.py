"""Opting an address out of marketing email.

The subscriber table doubles as the suppression list: ``is_active = False`` is
what ``broadcast_service`` and the content digest check before sending. So an
address that never subscribed still needs a row when it opts out — a registered
user who only ever received mail because they have an account has no
subscriber row at all, and without one, unsubscribing them would silently do
nothing and they would keep receiving every digest.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.newsletter import NewsletterSubscriber


async def unsubscribe(db: AsyncSession, email: str) -> bool:
    """Suppress an address. Returns True if this call changed anything.

    Idempotent: clicking an unsubscribe link twice is not an error, and the
    second click leaves the original timestamp alone.
    """
    address = email.strip().lower()
    existing = await db.scalar(
        select(NewsletterSubscriber).where(NewsletterSubscriber.email == address)
    )

    if existing is None:
        db.add(NewsletterSubscriber(
            email=address,
            is_active=False,
            unsubscribed_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        return True

    if not existing.is_active:
        return False

    existing.is_active = False
    existing.unsubscribed_at = datetime.now(timezone.utc)
    await db.commit()
    return True
