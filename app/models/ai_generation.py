import uuid
import enum
from datetime import datetime
from sqlalchemy import Boolean, Text, DateTime, Enum as SAEnum, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class AIProvider(str, enum.Enum):
    suno = "suno"
    self_hosted = "self_hosted"


class AIGenerationStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class AIGeneration(Base):
    __tablename__ = "ai_generations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=True)
    provider: Mapped[AIProvider] = mapped_column(SAEnum(AIProvider), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    style_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[AIGenerationStatus] = mapped_column(SAEnum(AIGenerationStatus), default=AIGenerationStatus.pending, nullable=False)
    result_loop_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("loops.id", ondelete="SET NULL"), nullable=True)
    is_extra: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
