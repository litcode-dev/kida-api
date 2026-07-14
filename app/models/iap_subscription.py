import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, func, Enum as SAEnum, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class IapPlatform(str, enum.Enum):
    ios = "ios"
    android = "android"


class IapSubscriptionStatus(str, enum.Enum):
    active = "active"
    grace = "grace"
    expired = "expired"
    canceled = "canceled"
    revoked = "revoked"


class IapSubscription(Base):
    """A store (App Store / Play) auto-renewable subscription entitlement.

    One row per user — the logical "Kiɗa Premium" entitlement. Switching plans
    (monthly ↔ yearly) or re-verifying updates this same row in place. Keyed for
    idempotency on ``store_transaction_id`` (Apple ``original_transaction_id`` or
    the Play purchase token).
    """

    __tablename__ = "iap_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    platform: Mapped[IapPlatform] = mapped_column(SAEnum(IapPlatform), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    store_transaction_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True, index=True)
    status: Mapped[IapSubscriptionStatus] = mapped_column(
        SAEnum(IapSubscriptionStatus), nullable=False, default=IapSubscriptionStatus.active
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_receipt: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
