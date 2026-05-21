from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.newsletter import NewsletterSubscriber
from app.schemas.common import success

router = APIRouter(prefix="/newsletter", tags=["newsletter"])


class SubscribeRequest(BaseModel):
    email: EmailStr


@router.post(
    "/subscribe",
    summary="Subscribe to newsletter",
    description="Subscribe an email address to Kida's news and marketing emails. Re-subscribing a previously unsubscribed address reactivates it.",
    responses={
        200: {"description": "Subscribed successfully"},
        422: {"description": "Invalid email address"},
    },
)
async def subscribe(body: SubscribeRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(
        select(NewsletterSubscriber).where(NewsletterSubscriber.email == body.email)
    )

    if existing:
        if existing.is_active:
            return success(message="Already subscribed", data={"email": body.email})
        existing.is_active = True
        existing.unsubscribed_at = None
        await db.commit()
        return success(message="Subscription reactivated", data={"email": body.email})

    subscriber = NewsletterSubscriber(email=body.email)
    db.add(subscriber)
    await db.commit()
    return success(message="Subscribed successfully", data={"email": body.email})


@router.post(
    "/unsubscribe",
    summary="Unsubscribe from newsletter",
    description="Unsubscribe an email address from Kida's news and marketing emails.",
    responses={
        200: {"description": "Unsubscribed successfully or address not found"},
        422: {"description": "Invalid email address"},
    },
)
async def unsubscribe(body: SubscribeRequest, db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone

    existing = await db.scalar(
        select(NewsletterSubscriber).where(NewsletterSubscriber.email == body.email)
    )

    if not existing or not existing.is_active:
        return success(message="Not subscribed", data={"email": body.email})

    existing.is_active = False
    existing.unsubscribed_at = datetime.now(timezone.utc)
    await db.commit()
    return success(message="Unsubscribed successfully", data={"email": body.email})
