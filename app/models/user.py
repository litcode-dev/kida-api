import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Integer, String, DateTime, Enum as SAEnum, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class UserRole(str, enum.Enum):
    user = "user"
    producer = "producer"
    admin = "admin"
    super_admin = "super_admin"


# Privilege order. A role outranks another only if its rank is strictly higher —
# two super admins cannot act on each other, which is what stops the top role
# from being self-destructing.
ROLE_RANK = {
    UserRole.user: 0,
    UserRole.producer: 1,
    UserRole.admin: 2,
    UserRole.super_admin: 3,
}

# Roles that reach the /admin endpoints.
ADMIN_ROLES = (UserRole.admin, UserRole.super_admin)


def outranks(actor: UserRole, target: UserRole) -> bool:
    return ROLE_RANK[actor] > ROLE_RANK[target]


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole), default=UserRole.user, nullable=False)
    onesignal_player_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    oauth_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    oauth_provider_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ai_extra_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Paid-for downloads beyond the monthly allowance. Spent one per item once
    # the month's quota for that type is used up.
    download_extra_credits: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    suspension_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Set when the user asks to delete their account. The row survives until the
    # grace window closes and the purge job removes it for real; until then the
    # account is unusable but restorable by logging back in.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
