import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# The queue a request moves through. "new" is where every submission starts;
# the other three are end or holding states an admin sets by hand.
LOOP_REQUEST_STATUSES = ("new", "in_progress", "fulfilled", "declined")


class LoopRequest(Base):
    """A reference track a user would like the catalogue to cover."""

    __tablename__ = "loop_requests"
    __table_args__ = (
        CheckConstraint(
            "request_type IN ('loop', 'stems')", name="ck_loop_requests_request_type"
        ),
        CheckConstraint(
            "status IN ('new', 'in_progress', 'fulfilled', 'declined')",
            name="ck_loop_requests_status",
        ),
        Index("ix_loop_requests_status_created_at", "status", "created_at"),
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
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="new", default="new"
    )
    # Null until someone moves the request off "new" — an unworked request has
    # no status history to record.
    status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
