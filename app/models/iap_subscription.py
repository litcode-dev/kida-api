import uuid
import enum
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, ForeignKey, func, Enum as SAEnum, Text
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


class IapSubscriptionSource(str, enum.Enum):
    """Where the entitlement came from: a store purchase, RevenueCat, or an admin grant."""

    store = "store"
    revenuecat = "revenuecat"
    admin = "admin"


class PremiumPlan(str, enum.Enum):
    """Billing period of a Kiɗa Premium entitlement."""

    monthly = "monthly"
    yearly = "yearly"


class IapSubscription(Base):
    """A Kiɗa Premium subscription entitlement.

    One row per user — the logical "Kiɗa Premium" entitlement. Switching plans
    (monthly ↔ yearly) or re-verifying updates this same row in place. Keyed for
    idempotency on ``store_transaction_id`` (Apple ``original_transaction_id`` or
    the Play purchase token).

    Rows normally originate from a store purchase, but an admin can also grant
    or revoke the entitlement directly (``source = admin``), which is how comps,
    support credits and manual takedowns are handled.
    """

    __tablename__ = "iap_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    # Null on admin grants — they do not come from a store.
    platform: Mapped[IapPlatform | None] = mapped_column(SAEnum(IapPlatform), nullable=True)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    store_transaction_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True, index=True)
    # RevenueCat's ``app_user_id`` for this subscriber (== our ``user_id`` when the
    # app calls Purchases.logIn(user.id)). Kept for cross-referencing RevenueCat
    # webhooks and dashboard lookups; null for store/admin-sourced rows.
    app_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[IapSubscriptionStatus] = mapped_column(
        SAEnum(IapSubscriptionStatus), nullable=False, default=IapSubscriptionStatus.active
    )
    source: Mapped[IapSubscriptionSource] = mapped_column(
        SAEnum(IapSubscriptionSource), nullable=False, default=IapSubscriptionSource.store
    )
    # Set when an admin deactivates the entitlement. Client receipt verification
    # refuses to restore a locked row, so a revocation cannot be undone by the
    # app re-posting its receipt. An admin activation clears it.
    admin_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    admin_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_receipt: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
