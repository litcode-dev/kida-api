import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.middleware.rate_limit import limiter
from app.models.purchase import Purchase
from app.models.user import User
from app.schemas.common import success
from app.services import iap_service
from app.exceptions import AppError
from typing import Literal

from pydantic import BaseModel

router = APIRouter(prefix="/purchases", tags=["purchases"])


class IAPVerifyRequest(BaseModel):
    item_id: uuid.UUID
    platform: Literal["ios", "android"]
    receipt: str


@router.get("/me")
async def get_owned_items(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return all item IDs owned by the current user via IAP."""
    result = await db.execute(
        select(Purchase.item_id).where(
            Purchase.user_id == user.id,
            Purchase.item_id.isnot(None),
        )
    )
    owned_ids = [str(row) for row in result.scalars().all()]
    return success({"owned_item_ids": owned_ids})


@router.post("/verify")
@limiter.limit("20/minute")
async def verify_purchase(
    request: Request,
    body: IAPVerifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Verify an IAP receipt with Apple/Google and record ownership."""

    # Verify receipt with the appropriate store
    if body.platform == "ios":
        transaction_id = await iap_service.verify_apple_receipt(body.receipt)
    else:
        transaction_id = await iap_service.verify_google_receipt(body.receipt)

    # Upsert — idempotent if the app retries after a network failure
    existing = await db.execute(
        select(Purchase).where(
            Purchase.user_id == user.id,
            Purchase.item_id == body.item_id,
        )
    )
    purchase = existing.scalar_one_or_none()

    if purchase is None:
        purchase = Purchase(
            user_id=user.id,
            item_id=body.item_id,
            platform=body.platform,
            receipt=body.receipt,
            iap_transaction_id=transaction_id,
            verified_at=datetime.now(timezone.utc),
        )
        db.add(purchase)
    else:
        purchase.iap_transaction_id = transaction_id
        purchase.verified_at = datetime.now(timezone.utc)

    await db.commit()
    return success({"item_id": str(body.item_id)}, "Purchase verified")
