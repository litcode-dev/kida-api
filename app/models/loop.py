import enum
import re
import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    String, Integer, Boolean, Numeric, Text,
    DateTime, Enum as SAEnum, ForeignKey, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base
from app.models.price_sync import PriceSyncMixin


def _search_key(value: str) -> str:
    """Lowercase, letters and digits only — "Lo-fi" and "lofi" compare equal."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


class Genre(str, enum.Enum):
    """Catalogue genres, shared by loops and stem packs.

    The column is a native Postgres enum whose labels are these *member names*
    (``afrobeat_worship``), while the API sends and accepts the *values*
    ("Afrobeat Worship"). Adding a member therefore needs a migration —
    ``ALTER TYPE genre ADD VALUE`` — or every write with the new genre fails.
    Postgres cannot drop an enum label, so a member added here is effectively
    permanent: removing one means rebuilding the type and remapping rows.
    """

    afrobeat = "Afrobeat"
    amapiano = "Amapiano"
    trap = "Trap"
    boom_bap = "Boom Bap"
    lo_fi = "Lo-fi"
    gospel = "Gospel"
    afrobeat_worship = "Afrobeat Worship"
    contemporary_worship = "Contemporary Worship"
    dancehall = "Dancehall"
    afrohouse = "Afrohouse"
    highlife_gospel = "Highlife Gospel"
    african_praise = "African Praise"
    drill = "Drill"
    seben = "Seben"
    reggae = "Reggae"
    highlife = "Highlife"
    soukous = "Soukous"
    rumba = "Rumba"
    afro_pop = "Afro Pop"
    hip_hop = "Hip Hop"
    rnb = "R&B"
    kompa = "Kompa"
    fuji = "Fuji"
    jazz = "Jazz"
    blues = "Blues"
    country = "Country"

    @classmethod
    def matching(cls, query: str) -> list["Genre"]:
        """Genres whose display value contains ``query``, loosely compared.

        Free-text search runs against what people see — "Afrobeat Worship" —
        not the stored label (``afrobeat_worship``), and nobody types
        punctuation the way the catalogue spells it. Comparing with everything
        but letters and digits stripped means "lofi" finds Lo-fi, "hip hop"
        finds Hip Hop and "rb" finds R&B, while "worship" still finds both
        worship genres.
        """
        needle = _search_key(query)
        if not needle:
            return []
        return [genre for genre in cls if needle in _search_key(genre.value)]


class TempoFeel(str, enum.Enum):
    slow = "slow"
    mid = "mid"
    fast = "fast"


class Loop(PriceSyncMixin, Base):
    __tablename__ = "loops"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(300), unique=True, index=True, nullable=False)
    genre: Mapped[Genre] = mapped_column(SAEnum(Genre), nullable=False, index=True)
    bpm: Mapped[int] = mapped_column(Integer, nullable=False)
    time_signature: Mapped[str] = mapped_column(
        String(16), default="4/4", server_default="4/4", nullable=False
    )
    key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    duration: Mapped[int] = mapped_column(Integer, nullable=False)
    tempo_feel: Mapped[TempoFeel] = mapped_column(SAEnum(TempoFeel), nullable=False)
    # Postgres ARRAY, not the generic one: the tag filter uses .overlap(),
    # which is a dialect operator the generic type does not carry. The DDL
    # is identical either way, so this needs no migration.
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    is_free: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    store_product_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    preview_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thumbnail_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    aes_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    aes_iv: Mapped[str | None] = mapped_column(Text, nullable=True)
    waveform_data: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ready", server_default="ready", nullable=False)
    # When this loop went into a new-content digest. NULL means it is still
    # waiting to be announced; stamped once so a retried digest cannot email
    # the whole list about the same loop twice.
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
