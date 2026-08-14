import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field, field_validator, model_validator
from app.models.loop import Genre
from app.models.stem_pack import SongSection, StemInstrument, StemPackType

DOWNLOAD_EXPIRY_SECONDS = 900  # 15 minutes

MAX_ARRANGEMENT_ITEMS = 100
MAX_PART_REPEATS = 16


class StemPackCreate(BaseModel):
    title: str
    loop_id: uuid.UUID | None = None
    genre: Genre
    # Long-form by default so existing clients, which never sent this field,
    # keep creating the kind of pack they always did.
    pack_type: StemPackType = StemPackType.long_form
    bpm: int
    key: str
    tags: list[str] = []
    price: Decimal
    description: str | None = None


class StemPackUpdate(BaseModel):
    title: str | None = None
    loop_id: uuid.UUID | None = None
    genre: Genre | None = None
    bpm: int | None = None
    key: str | None = None
    tags: list[str] | None = None
    price: Decimal | None = None
    description: str | None = None
    # pack_type is deliberately absent: switching a pack between long-form and
    # breakdown would orphan every stem already uploaded under the old layout.


class StemCreate(BaseModel):
    instrument: StemInstrument
    # Free text shown in the app ("Rhodes", "Lead vox"). Defaults to the
    # instrument's own name when the producer does not supply one.
    label: str | None = None
    # Required for a breakdown pack, rejected for a long-form one.
    part_id: uuid.UUID | None = None

    @field_validator("label")
    @classmethod
    def blank_label_is_none(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


class StemPartCreate(BaseModel):
    section: SongSection
    name: str | None = None
    position: int | None = None
    bars: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def blank_name_is_none(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


class StemPartUpdate(BaseModel):
    section: SongSection | None = None
    name: str | None = None
    position: int | None = None
    bars: int | None = Field(default=None, gt=0)


class ArrangementItemCreate(BaseModel):
    part_id: uuid.UUID
    repeat_count: int = Field(default=1, ge=1, le=MAX_PART_REPEATS)


class StemArrangementCreate(BaseModel):
    name: str
    description: str | None = None
    is_default: bool = False
    items: list[ArrangementItemCreate]

    @field_validator("items")
    @classmethod
    def at_least_one_item(cls, v: list[ArrangementItemCreate]) -> list[ArrangementItemCreate]:
        if not v:
            raise ValueError("An arrangement needs at least one part")
        if len(v) > MAX_ARRANGEMENT_ITEMS:
            raise ValueError(f"An arrangement can have at most {MAX_ARRANGEMENT_ITEMS} entries")
        return v


class StemArrangementUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_default: bool | None = None
    # Sending items re-writes the whole running order and re-renders; omitting
    # them edits the metadata only and leaves the rendered tracks alone.
    items: list[ArrangementItemCreate] | None = None

    @model_validator(mode="after")
    def items_not_empty(self) -> "StemArrangementUpdate":
        if self.items is not None and not self.items:
            raise ValueError("An arrangement needs at least one part")
        if self.items is not None and len(self.items) > MAX_ARRANGEMENT_ITEMS:
            raise ValueError(f"An arrangement can have at most {MAX_ARRANGEMENT_ITEMS} entries")
        return self


class StemResponse(BaseModel):
    id: uuid.UUID
    instrument: StemInstrument
    label: str
    part_id: uuid.UUID | None = None
    duration: int
    status: str
    preview_url: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class StemPartResponse(BaseModel):
    id: uuid.UUID
    section: SongSection
    name: str
    position: int
    bars: int | None = None
    stems: list[StemResponse] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class ArrangementItemResponse(BaseModel):
    id: uuid.UUID
    part_id: uuid.UUID
    position: int
    repeat_count: int
    # Filled in by the router so a client can render the running order without
    # a second lookup of the parts.
    part_name: str | None = None
    section: SongSection | None = None

    model_config = {"from_attributes": True}


class StemArrangementTrackResponse(BaseModel):
    id: uuid.UUID
    instrument: StemInstrument
    duration: int
    status: str
    preview_url: str | None = None

    model_config = {"from_attributes": True}


class StemArrangementResponse(BaseModel):
    id: uuid.UUID
    stem_pack_id: uuid.UUID
    name: str
    description: str | None = None
    is_default: bool
    status: str
    duration: int
    items: list[ArrangementItemResponse] = []
    tracks: list[StemArrangementTrackResponse] = []
    created_at: datetime
    rendered_at: datetime | None = None

    model_config = {"from_attributes": True}


class StemPackResponse(BaseModel):
    id: uuid.UUID
    title: str
    slug: str
    loop_id: uuid.UUID | None
    genre: Genre
    pack_type: StemPackType
    bpm: int
    key: str
    tags: list[str]
    price: Decimal
    description: str | None
    # Long-form packs list their instrument stems here; breakdown packs carry
    # theirs inside `parts` and leave this empty.
    stems: list[StemResponse] = []
    parts: list[StemPartResponse] = []
    arrangements: list[StemArrangementResponse] = []
    created_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def breakdown_stems_live_under_their_parts(self) -> "StemPackResponse":
        # Dropped here rather than in the query, because a breakdown pack's
        # stems are the same rows already listed inside `parts` — emptying the
        # loaded relationship instead would look like orphaning them to the
        # session, and delete-orphan would take that literally.
        if self.pack_type == StemPackType.breakdown:
            self.stems = []
        return self


class StemPackFilter(BaseModel):
    # Public callers set this; producer and admin listings leave it off so a
    # producer can still watch their own uploads move through processing.
    ready_only: bool = False
    search: str | None = None
    genre: Genre | None = None
    pack_type: StemPackType | None = None
    tags: list[str] | None = None
    page: int = 1
    page_size: int = 20
    created_by: uuid.UUID | None = None

    @field_validator("page_size")
    @classmethod
    def cap_page_size(cls, v: int) -> int:
        return min(v, 100)


class StemDownloadItem(BaseModel):
    id: uuid.UUID
    instrument: StemInstrument
    label: str
    signed_url: str
    aes_key: str
    aes_iv: str
    duration: int
    part_id: uuid.UUID | None = None
    part_name: str | None = None
    expires_in_seconds: int = DOWNLOAD_EXPIRY_SECONDS


class StemPackDownloadResponse(BaseModel):
    pack_id: uuid.UUID
    title: str
    pack_type: StemPackType
    stems: list[StemDownloadItem]
    expires_in_seconds: int = DOWNLOAD_EXPIRY_SECONDS


class ArrangementTrackDownloadItem(BaseModel):
    id: uuid.UUID
    instrument: StemInstrument
    signed_url: str
    aes_key: str
    aes_iv: str
    duration: int
    expires_in_seconds: int = DOWNLOAD_EXPIRY_SECONDS


class StemArrangementDownloadResponse(BaseModel):
    pack_id: uuid.UUID
    arrangement_id: uuid.UUID
    name: str
    duration: int
    tracks: list[ArrangementTrackDownloadItem]
    expires_in_seconds: int = DOWNLOAD_EXPIRY_SECONDS
