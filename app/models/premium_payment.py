import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.purchase import PaymentProvider


class PremiumPayment(Base):
    """One card payment that bought a prepaid period of Kiɗa Premium.

    The entitlement itself lives on the user's single ``IapSubscription`` row,
    which every premium check reads. That row only remembers its latest state,
    so this is the ledger behind it: one row per payment, unique on the
    gateway reference, which is what stops the webhook and the client's own
    confirm call from both adding the same 30 days.
    """

    __tablename__ = "premium_payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        SAEnum(PaymentProvider), nullable=False
    )
    reference: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # "monthly" or "yearly" (PremiumPlan). A plain string rather than a new
    # database enum, so adding a plan later needs no migration.
    plan: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # The period this payment paid for. Starts at the old expiry when the user
    # renewed early, so the days they had left are not lost.
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
