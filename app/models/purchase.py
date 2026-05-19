import uuid
import enum
from datetime import datetime
from decimal import Decimal
from sqlalchemy import String, Numeric, DateTime, ForeignKey, func, Enum as SAEnum, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class PurchaseType(str, enum.Enum):
    one_time = "one_time"


class PaymentProvider(str, enum.Enum):
    flutterwave = "flutterwave"
    paystack = "paystack"


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)

    # Legacy web-payment fields (nullable so IAP rows don't need them)
    loop_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("loops.id"), nullable=True, index=True)
    stem_pack_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stem_packs.id"), nullable=True, index=True)
    drone_pad_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("drone_pads.id"), nullable=True, index=True)
    drum_kit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("drum_kits.id"), nullable=True, index=True)
    payment_reference: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    payment_provider: Mapped[PaymentProvider | None] = mapped_column(SAEnum(PaymentProvider), nullable=True)
    amount_paid: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    purchase_type: Mapped[PurchaseType] = mapped_column(SAEnum(PurchaseType), default=PurchaseType.one_time)

    # IAP fields
    item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    platform: Mapped[str | None] = mapped_column(String(10), nullable=True)
    receipt: Mapped[str | None] = mapped_column(Text, nullable=True)
    iap_transaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
