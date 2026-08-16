"""Recipient resolution for admin broadcast emails.

Kept separate from the sending task so the audience rules can be tested — and
read — without a mail backend.

Unsubscribes are honoured for every audience. A registered user who unsubscribed
from the newsletter is excluded even from the ``users`` audience: they asked not
to receive marketing, and having an account is not consent to it. Transactional
mail (verification, receipts, deletion notices) does not come through here and is
unaffected.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.newsletter import NewsletterSubscriber
from app.models.user import User
from app.schemas.broadcast import BroadcastAudience


async def _unsubscribed_emails(db: AsyncSession) -> set[str]:
    rows = await db.scalars(
        select(NewsletterSubscriber.email).where(NewsletterSubscriber.is_active.is_(False))
    )
    return {e.lower() for e in rows}


async def resolve_recipients(db: AsyncSession, audience: BroadcastAudience) -> list[str]:
    """The deduplicated address list a broadcast should go to, sorted for stable output."""
    emails: set[str] = set()

    if audience in (BroadcastAudience.users, BroadcastAudience.all):
        emails |= {e.lower() for e in await db.scalars(select(User.email))}

    if audience in (BroadcastAudience.subscribers, BroadcastAudience.all):
        emails |= {
            e.lower()
            for e in await db.scalars(
                select(NewsletterSubscriber.email).where(
                    NewsletterSubscriber.is_active.is_(True)
                )
            )
        }

    return sorted(emails - await _unsubscribed_emails(db))


async def count_recipients(db: AsyncSession, audience: BroadcastAudience) -> int:
    return len(await resolve_recipients(db, audience))
