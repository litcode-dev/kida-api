import re
import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field, field_validator, model_validator
from app.models.loop import Genre, TempoFeel

TIME_SIGNATURE_RE = re.compile(r"[1-9]\d?/(1|2|4|8|16|32)")

# The ceiling was 140, which pre-dates half the catalogue: drill sits around
# 140-145, seben and soukous run past 150, and anything counted in double time
# reads higher still. 250 covers those without letting a typo through.
BPM_MIN = 60
BPM_MAX = 250


class LoopCreate(BaseModel):
    title: str
    description: str | None = None
    genre: Genre
    bpm: int
    time_signature: str = "4/4"
    tempo_feel: TempoFeel
    tags: list[str] = []
    price: Decimal
    is_free: bool = False
    desired_price_usd: Decimal | None = Field(default=None, gt=0)

    @field_validator("bpm")
    @classmethod
    def bpm_range(cls, v: int) -> int:
        if not (BPM_MIN <= v <= BPM_MAX):
            raise ValueError(f"BPM must be between {BPM_MIN} and {BPM_MAX}")
        return v

    @field_validator("time_signature")
    @classmethod
    def time_signature_format(cls, v: str) -> str:
        value = v.strip()
        if not TIME_SIGNATURE_RE.fullmatch(value):
            raise ValueError(
                "Time signature must be in numerator/denominator format, e.g. 4/4"
            )
        return value


class LoopUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    genre: Genre | None = None
    bpm: int | None = None
    time_signature: str | None = None
    tempo_feel: TempoFeel | None = None
    tags: list[str] | None = None
    price: Decimal | None = None
    is_free: bool | None = None
    desired_price_usd: Decimal | None = Field(default=None, gt=0)

    @field_validator("bpm")
    @classmethod
    def bpm_range(cls, v: int | None) -> int | None:
        if v is not None and not (BPM_MIN <= v <= BPM_MAX):
            raise ValueError(f"BPM must be between {BPM_MIN} and {BPM_MAX}")
        return v

    @field_validator("time_signature")
    @classmethod
    def time_signature_format(cls, v: str | None) -> str | None:
        if v is None:
            return v
        value = v.strip()
        if not TIME_SIGNATURE_RE.fullmatch(value):
            raise ValueError(
                "Time signature must be in numerator/denominator format, e.g. 4/4"
            )
        return value


class LoopResponse(BaseModel):
    id: uuid.UUID
    title: str
    slug: str
    genre: Genre
    bpm: int
    time_signature: str
    key: str | None = None
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
    # Public callers set this; producer and admin listings leave it off so a
    # producer can still watch their own uploads move through processing.
    ready_only: bool = False
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
    created_by: uuid.UUID | None = None
