import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field, field_validator, model_validator
from app.models.loop import Genre, TempoFeel


class LoopCreate(BaseModel):
    title: str
    description: str | None = None
    genre: Genre
    bpm: int
    key: str | None = None
    tempo_feel: TempoFeel
    tags: list[str] = []
    price: Decimal
    is_free: bool = False

    @field_validator("bpm")
    @classmethod
    def bpm_range(cls, v: int) -> int:
        if not (60 <= v <= 140):
            raise ValueError("BPM must be between 60 and 140")
        return v


class LoopUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    genre: Genre | None = None
    bpm: int | None = None
    key: str | None = None
    tempo_feel: TempoFeel | None = None
    tags: list[str] | None = None
    price: Decimal | None = None
    is_free: bool | None = None

    @field_validator("bpm")
    @classmethod
    def bpm_range(cls, v: int | None) -> int | None:
        if v is not None and not (60 <= v <= 140):
            raise ValueError("BPM must be between 60 and 140")
        return v


class LoopResponse(BaseModel):
    id: uuid.UUID
    title: str
    slug: str
    genre: Genre
    bpm: int
    key: str
    duration: int
    tempo_feel: TempoFeel
    description: str | None = None
    tags: list[str]
    price: Decimal
    is_free: bool
    is_paid: bool
    store_product_id: str | None = None
    preview_s3_key: str | None = Field(default=None, exclude=True)
    thumbnail_s3_key: str | None = Field(default=None, exclude=True)
    preview_url: str | None = None
    thumbnail_url: str | None = None
    waveform_data: list | None
    download_count: int
    play_count: int
    created_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def build_urls(self) -> "LoopResponse":
        from app.config import get_settings
        base = get_settings().s3_cloudfront_url.rstrip("/")
        if self.preview_s3_key:
            self.preview_url = f"{base}/{self.preview_s3_key}" if base else self.preview_s3_key
        if self.thumbnail_s3_key:
            self.thumbnail_url = f"{base}/{self.thumbnail_s3_key}" if base else self.thumbnail_s3_key
        return self


class LoopFilter(BaseModel):
    search: str | None = None
    genre: Genre | None = None
    bpm_min: int | None = None
    bpm_max: int | None = None
    key: str | None = None
    tempo_feel: TempoFeel | None = None
    tags: list[str] | None = None
    is_free: bool | None = None
    sort: str = "newest"
    page: int = 1
    page_size: int = 20
