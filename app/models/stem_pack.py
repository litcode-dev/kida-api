import uuid
import enum
from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    String, Integer, Numeric, Text, DateTime, ForeignKey, func, ARRAY,
    Boolean, Index, UniqueConstraint, Enum as SAEnum, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from app.models.loop import Genre


def _enum(enum_cls, name: str) -> SAEnum:
    """A Postgres enum that stores the member *values*, not their Python names.

    Names and values are identical for every enum here, but spelling it out
    keeps the column readable in psql and matches how DronePad.key is mapped.
    """
    return SAEnum(enum_cls, name=name, values_callable=lambda obj: [e.value for e in obj])


class StemPackType(str, enum.Enum):
    """How a pack's audio is laid out — chosen once, at upload time.

    ``long_form`` is one continuous take per instrument: the whole song in a
    single file each for drums, piano, bass and so on.

    ``breakdown`` is the same song cut into its parts (intro, verse, chorus…).
    Every part is uploaded per instrument, and an arrangement later names the
    order the parts play in. Rendering an arrangement stitches its parts back
    into one long file per instrument, which is what the buyer downloads.
    """

    long_form = "long_form"
    breakdown = "breakdown"


class StemInstrument(str, enum.Enum):
    """The instrument an individual stem carries.

    ``cue`` is the spoken/count-in guide track and ``metronome`` the click —
    both ship alongside the musical parts so a live band can play to them.
    """

    drums = "drums"
    piano = "piano"
    guitar = "guitar"
    bass_guitar = "bass_guitar"
    vocal = "vocal"
    cue = "cue"
    metronome = "metronome"
    others = "others"


class SongSection(str, enum.Enum):
    """The musical role of a part in a breakdown pack."""

    intro = "intro"
    verse = "verse"
    pre_chorus = "pre_chorus"
    chorus = "chorus"
    hook = "hook"
    refrain = "refrain"
    bridge = "bridge"
    vamp = "vamp"
    interlude = "interlude"
    instrumental = "instrumental"
    solo = "solo"
    outro = "outro"
    other = "other"


class StemPack(Base):
    __tablename__ = "stem_packs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(300), unique=True, index=True, nullable=False)
    loop_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("loops.id", ondelete="SET NULL"), nullable=True, index=True)
    genre: Mapped[Genre] = mapped_column(SAEnum(Genre), nullable=False, index=True)
    pack_type: Mapped[StemPackType] = mapped_column(
        _enum(StemPackType, "stem_pack_type"),
        nullable=False,
        default=StemPackType.long_form,
        server_default=StemPackType.long_form.value,
        index=True,
    )
    bpm: Mapped[int] = mapped_column(Integer, nullable=False)
    key: Mapped[str] = mapped_column(String(50), nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    stems: Mapped[list["Stem"]] = relationship(
        "Stem", back_populates="stem_pack", lazy="noload", cascade="all, delete-orphan"
    )
    parts: Mapped[list["StemPart"]] = relationship(
        "StemPart",
        back_populates="stem_pack",
        lazy="noload",
        cascade="all, delete-orphan",
        order_by="StemPart.position",
    )
    arrangements: Mapped[list["StemArrangement"]] = relationship(
        "StemArrangement", back_populates="stem_pack", lazy="noload", cascade="all, delete-orphan"
    )

    @property
    def is_breakdown(self) -> bool:
        return self.pack_type == StemPackType.breakdown


class StemPart(Base):
    """One section of a breakdown pack — "Verse 1", "Chorus", "Outro".

    A part is the unit an arrangement is written in: it exists once per pack
    and holds one stem per instrument that plays during it. Long-form packs
    have no parts at all.
    """

    __tablename__ = "stem_parts"
    __table_args__ = (
        UniqueConstraint("stem_pack_id", "name", name="uq_stem_parts_pack_name"),
        Index("ix_stem_parts_pack_position", "stem_pack_id", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stem_pack_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stem_packs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    section: Mapped[SongSection] = mapped_column(_enum(SongSection, "song_section"), nullable=False)
    # Distinguishes repeats of the same section: "Verse 1" and "Verse 2" are
    # both section=verse. Unique within the pack so arrangements read clearly.
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Display order of the parts as uploaded, which is not necessarily the
    # order they play in — that is what an arrangement decides.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    stem_pack: Mapped["StemPack"] = relationship("StemPack", back_populates="parts")
    stems: Mapped[list["Stem"]] = relationship(
        "Stem", back_populates="part", lazy="noload", cascade="all, delete-orphan"
    )


class Stem(Base):
    """One audio file: a single instrument, either whole-song or one part of it.

    ``part_id`` is what separates the two upload modes — NULL for a long-form
    stem (the instrument's entire take), set for a breakdown stem (that
    instrument during that one part).
    """

    __tablename__ = "stems"
    __table_args__ = (
        # One stem per instrument per part…
        UniqueConstraint("part_id", "instrument", name="uq_stems_part_instrument"),
        # …and one per instrument per pack for long-form stems. That needs a
        # partial index rather than a plain constraint: a breakdown pack has
        # many stems per instrument, and NULL part_ids never collide anyway.
        Index(
            "uq_stems_pack_instrument_long_form",
            "stem_pack_id", "instrument",
            unique=True,
            postgresql_where=text("part_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stem_pack_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stem_packs.id", ondelete="CASCADE"), nullable=False, index=True)
    part_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stem_parts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    instrument: Mapped[StemInstrument] = mapped_column(
        _enum(StemInstrument, "stem_instrument"),
        nullable=False,
        default=StemInstrument.others,
        server_default=StemInstrument.others.value,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    file_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    preview_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    aes_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    aes_iv: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="processing", server_default="processing"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    stem_pack: Mapped["StemPack"] = relationship("StemPack", back_populates="stems")
    part: Mapped["StemPart | None"] = relationship("StemPart", back_populates="stems")


class StemArrangement(Base):
    """An ordered play-through of a breakdown pack's parts.

    The arrangement itself is just the running order; rendering it produces one
    continuous file per instrument (``StemArrangementTrack``), which is what a
    buyer downloads and drops into a session.
    """

    __tablename__ = "stem_arrangements"
    __table_args__ = (
        UniqueConstraint("stem_pack_id", "name", name="uq_stem_arrangements_pack_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stem_pack_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stem_packs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The arrangement offered when a client asks to download the pack itself.
    # At most one per pack; enforced in the service, not the schema, because
    # clearing the old default and setting the new one is a single operation.
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # "draft" until a render is queued, then "rendering" → "ready" | "failed".
    # A ready arrangement goes "stale" when the audio underneath it changes,
    # which leaves the old tracks downloadable but flags a re-render.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )
    duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    rendered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stem_pack: Mapped["StemPack"] = relationship("StemPack", back_populates="arrangements")
    items: Mapped[list["StemArrangementItem"]] = relationship(
        "StemArrangementItem",
        back_populates="arrangement",
        lazy="noload",
        cascade="all, delete-orphan",
        order_by="StemArrangementItem.position",
    )
    tracks: Mapped[list["StemArrangementTrack"]] = relationship(
        "StemArrangementTrack",
        back_populates="arrangement",
        lazy="noload",
        cascade="all, delete-orphan",
    )


class StemArrangementItem(Base):
    """One slot in the running order: play this part, this many times."""

    __tablename__ = "stem_arrangement_items"
    __table_args__ = (
        UniqueConstraint("arrangement_id", "position", name="uq_stem_arrangement_items_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    arrangement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stem_arrangements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    part_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stem_parts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # A chorus played twice back to back is one item with repeat_count=2 rather
    # than two items, so the running order reads the way a chart does.
    repeat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    arrangement: Mapped["StemArrangement"] = relationship("StemArrangement", back_populates="items")
    part: Mapped["StemPart"] = relationship("StemPart", lazy="noload")


class StemArrangementTrack(Base):
    """A rendered arrangement, one row per instrument.

    Same shape as a stem — encrypted file, preview, AES key — because clients
    consume it through exactly the same download path.
    """

    __tablename__ = "stem_arrangement_tracks"
    __table_args__ = (
        UniqueConstraint("arrangement_id", "instrument", name="uq_stem_arrangement_tracks_instrument"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    arrangement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stem_arrangements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instrument: Mapped[StemInstrument] = mapped_column(
        _enum(StemInstrument, "stem_instrument"), nullable=False
    )
    file_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    preview_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    aes_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    aes_iv: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="rendering", server_default="rendering"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    arrangement: Mapped["StemArrangement"] = relationship("StemArrangement", back_populates="tracks")
