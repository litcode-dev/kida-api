import uuid
import enum
from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    String, Integer, Boolean, Numeric, Text,
    DateTime, Enum as SAEnum, ForeignKey, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from app.models.price_sync import PriceSyncMixin


class MusicalKey(str, enum.Enum):
    C = "C"
    C_sharp = "C#"
    D = "D"
    D_sharp = "D#"
    E = "E"
    F = "F"
    F_sharp = "F#"
    G = "G"
    G_sharp = "G#"
    A = "A"
    A_sharp = "A#"
    B = "B"


class DronePadCategory(Base):
    __tablename__ = "drone_pad_categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    drones: Mapped[list["Drone"]] = relationship("Drone", back_populates="category", lazy="noload")


class Drone(PriceSyncMixin, Base):
    __tablename__ = "drones"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    is_free: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    store_product_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("drone_pad_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category: Mapped["DronePadCategory | None"] = relationship(
        "DronePadCategory", back_populates="drones", lazy="noload"
    )
    pads: Mapped[list["DronePad"]] = relationship(
        "DronePad",
        back_populates="drone",
        cascade="all, delete-orphan",
        lazy="noload",
        order_by="DronePad.key",
    )


class DronePad(Base):
    __tablename__ = "drone_pads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    drone_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drones.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[MusicalKey] = mapped_column(
        SAEnum(MusicalKey, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )
    duration: Mapped[int] = mapped_column(Integer, nullable=False)
    preview_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    preview_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thumbnail_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    aes_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    aes_iv: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="ready", server_default="ready", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    drone: Mapped["Drone"] = relationship("Drone", back_populates="pads", lazy="noload")

    @property
    def title(self) -> str | None:
        return self.drone.title if self.drone else None

    @property
    def description(self) -> str | None:
        return self.drone.description if self.drone else None

    @property
    def price(self) -> Decimal | None:
        return self.drone.price if self.drone else None

    @property
    def is_free(self) -> bool:
        return self.drone.is_free if self.drone else False

    @property
    def store_product_id(self) -> str | None:
        return self.drone.store_product_id if self.drone else None

    @property
    def download_count(self) -> int:
        return self.drone.download_count if self.drone else 0

    @property
    def category_id(self) -> uuid.UUID | None:
        return self.drone.category_id if self.drone else None

    @property
    def category(self) -> "DronePadCategory | None":
        return self.drone.category if self.drone else None

    @property
    def parent_thumbnail_url(self) -> str | None:
        return self.drone.thumbnail_url if self.drone else None
