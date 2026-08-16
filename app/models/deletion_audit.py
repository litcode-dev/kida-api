"""The record that a deletion happened, and the work queue that finishes it.

Two tables, deliberately split:

* :class:`AccountDeletionAudit` is the permanent trail. It answers "was this
  account deleted, when, at whose request, and did the third parties confirm" —
  and it holds **no personal data at all**. The subject is identified by an HMAC
  of the user id, so support can verify a specific claim ("here is my user id,
  prove you deleted me") without the table itself being a list of who used the
  app. There is no email column to forget to clear.

* :class:`AccountDeletionPropagation` is transient. Telling RevenueCat and
  OneSignal to delete someone requires knowing what *they* call that person, so
  those identifiers have to survive the local delete long enough to be sent.
  The row is destroyed the moment propagation succeeds or is abandoned, which is
  why it lives here and not in the audit table.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Enum as SAEnum, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DeletionActor(str, enum.Enum):
    """Who asked for the deletion."""

    user = "user"                    # DELETE /auth/me from the signed-in app
    admin = "admin"                  # an admin removed the account
    email_request = "email_request"  # confirmed via the public deletion-request link


class PropagationStatus(str, enum.Enum):
    pending = "pending"    # not attempted yet, or attempted and retryable
    done = "done"          # the third party confirmed (or had nothing to delete)
    skipped = "skipped"    # not configured, or we hold no identifier for them
    failed = "failed"      # retries exhausted — needs a human


class AccountDeletionAudit(Base):
    """One permanent row per account deletion. Contains no personal data."""

    __tablename__ = "account_deletion_audit"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # HMAC-SHA256(secret_key, "kida:deletion-audit:v1|<user_id>") as hex.
    # Not reversible into a user id, but recomputable from one, which is exactly
    # what a "did you really delete me?" enquiry needs.
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    actor: Mapped[DeletionActor] = mapped_column(
        SAEnum(DeletionActor, name="deletion_actor"), nullable=False
    )
    # When the account holder asked. Null for an admin removal, which nobody
    # asked for. Deletion is immediate, so this sits alongside ``purged_at``.
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the rows actually went away.
    purged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    revenuecat_status: Mapped[PropagationStatus] = mapped_column(
        SAEnum(PropagationStatus, name="propagation_status"),
        default=PropagationStatus.pending,
        nullable=False,
    )
    onesignal_status: Mapped[PropagationStatus] = mapped_column(
        SAEnum(PropagationStatus, name="propagation_status"),
        default=PropagationStatus.pending,
        nullable=False,
    )
    # Failure text only — never an identifier. Truncated by the writer.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class AccountDeletionPropagation(Base):
    """Third-party identifiers held only until propagation finishes.

    Deleted on success or on abandonment, so the steady state of this table is
    empty. Anything sitting here is unfinished work, which makes a stuck
    deletion visible by ``SELECT count(*)`` rather than by log archaeology.
    """

    __tablename__ = "account_deletion_propagation"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account_deletion_audit.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Every id RevenueCat might know this person by, newline-separated. Plural
    # because the app user id has not been consistent: the backend reconciles
    # with the user UUID, while the client has been reporting the email address.
    # See revenuecat_service.delete_subscriber.
    revenuecat_app_user_ids: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # The OneSignal subscription (legacy "player") id we had on file.
    onesignal_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Our user id, used as the OneSignal external_id alias when the app sets one.
    onesignal_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
