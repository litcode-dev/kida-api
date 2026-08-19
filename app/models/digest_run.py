"""One row per attempt at the daily new-content digest.

The digest used to leave no trace of itself. If no mail arrived there was
nothing to look at: a beat process that never started, a sweep that found
nothing, and a send that failed silently against an unconfigured mail backend
all looked identical from the outside — an empty log and an inbox with nothing
in it.

Every run now writes a row here, and the row is also the lock. ``run_key`` is
the scheduled moment the run belongs to ("2026-08-19T17"), and it is unique, so
whichever process gets the insert owns that run: celery beat and the API's own
scheduler can both be pointed at the digest without the list ever getting two
copies.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DigestRunStatus(str):
    """Why a run ended. Plain strings — this is a log, not a state machine."""

    running = "running"                  # claimed, still going (or died mid-run)
    sent = "sent"                        # mail handed to the provider
    empty = "empty"                      # nothing new was ready to announce
    no_recipients = "no_recipients"      # nobody to send to
    not_configured = "not_configured"    # mail backend has no credentials
    failed = "failed"                    # the send itself failed


class DigestRun(Base):
    __tablename__ = "digest_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # The scheduled slot this run belongs to, e.g. "2026-08-19T17". Unique: it
    # is what stops two schedulers from sending the same digest twice. Manual
    # runs carry their own timestamped key so they never consume a daily slot.
    run_key: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Which entry point started it: "beat", "api" (the in-process scheduler),
    # "manual" (admin endpoint or script). Answers "is beat actually running?".
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=DigestRunStatus.running
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    recipients: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    sent: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # What went out, by type: {"loop": ["<uuid>", ...]}. Kept so a digest lost
    # to a failed send can be reconstructed by hand from the row alone.
    claimed_ids: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
