import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, func, Enum as SAEnum, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class DownloadGrantType(str, enum.Enum):
    loop = "loop"
    drum_kit = "drum_kit"
    drone_pad = "drone_pad"
    drone_group = "drone_group"


class DownloadGrant(Base):
    """A lifetime free-tier download slot consumed by a non-subscriber.

    One row per (user, item type, item) — re-downloads of an already-granted
    item are idempotent and never consume a new slot. Deleting a file on the
    device does not free a slot; the server cap is anti-abuse, the friendlier
    slot-based behavior lives client-side.
    """

    __tablename__ = "download_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "item_type", "item_id", name="uq_download_grants_user_item"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_type: Mapped[DownloadGrantType] = mapped_column(SAEnum(DownloadGrantType), nullable=False)
    # UUID string for loops/drum kits/drone groups; "<drone_id>:<key>" for drone
    # pads so the same musical pad is one grant however it is fetched.
    item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
