import uuid
from datetime import datetime
from typing import Literal
from pydantic import BaseModel
from app.models.subscription import SubscriptionPlan, SubscriptionStatus
from app.models.purchase import PaymentProvider


class SubscriptionInitiateRequest(BaseModel):
    provider: PaymentProvider


class IapVerifyRequest(BaseModel):
    """Store-subscription verification payload sent by the Kiɗa app."""

    product_id: str
    platform: Literal["ios", "android"]
    receipt: str


class EntitlementResponse(BaseModel):
    active: bool
    expires_at: str | None = None
    product_id: str | None = None


class ExtraCreditsInitiateRequest(BaseModel):
    provider: PaymentProvider


class SubscriptionResponse(BaseModel):
    id: uuid.UUID
    plan: SubscriptionPlan
    status: SubscriptionStatus
    ai_quota: int
    ai_quota_used: int
    billing_period_start: datetime
    expires_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}
