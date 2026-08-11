import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MonthlyQuotaType(str, enum.Enum):
    """The three things a monthly download allowance is counted in."""

    loop = "loop"
    drone = "drone"
    drum_kit = "drum_kit"


class MonthlyDownloadUsage(Base):
    """One row per distinct item a user downloaded in a calendar month.

    Rows rather than a counter, because the allowance is 20 *loops* a month, not
    20 download requests: re-fetching something already taken this month must not
    cost another slot (the app re-downloads when files go missing on the device).
    Uniqueness on (user, period, type, item) makes that idempotent.

    This cannot be counted from the downloads table — those rows are deleted by
    cleanup_expired_downloads roughly an hour after they are written.
    """

    __tablename__ = "monthly_download_usage"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "period", "item_type", "item_id",
            name="uq_monthly_download_usage_user_period_item",
        ),
        Index("ix_monthly_usage_user_period_type", "user_id", "period", "item_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Calendar month in UTC as "YYYY-MM". A plain string keeps the unique
    # constraint and the count query trivial, and reads clearly in the database.
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    item_type: Mapped[MonthlyQuotaType] = mapped_column(SAEnum(MonthlyQuotaType), nullable=False)
    # Loop, drone group or drum kit id. A drone group counts once however many of
    # its pads are fetched.
    item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
