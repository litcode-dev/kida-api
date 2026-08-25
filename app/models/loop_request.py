import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LoopRequest(Base):
    """A reference track a user would like the catalogue to cover."""

    __tablename__ = "loop_requests"
    __table_args__ = (
        CheckConstraint(
            "request_type IN ('loop', 'stems')", name="ck_loop_requests_request_type"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    request_type: Mapped[str] = mapped_column(String(16), nullable=False)
    artist_name: Mapped[str] = mapped_column(String(255), nullable=False)
    song_title: Mapped[str] = mapped_column(String(255), nullable=False)
    reference_link: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
