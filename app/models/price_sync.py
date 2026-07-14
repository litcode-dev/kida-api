import enum
from datetime import datetime
from decimal import Decimal
from sqlalchemy import String, Numeric, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column


class PriceSyncState(str, enum.Enum):
    pending = "pending"
    apple_pending_review = "apple_pending_review"
    synced = "synced"
    error = "error"


class PriceSyncMixin:
    """Store-price sync columns shared by every sellable item (loops, drum kits, drones).

    The stores are the source of truth for IAP prices: an admin sets
    desired_price_usd here and the reconcile worker pushes it to Google Play
    and the App Store, tracking progress in price_sync_state.
    """

    desired_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_sync_state: Mapped[str] = mapped_column(
        String(30),
        default=PriceSyncState.pending.value,
        server_default=PriceSyncState.pending.value,
        nullable=False,
    )
    price_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
