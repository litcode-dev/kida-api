import uuid
from datetime import datetime

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class LoopRequestCreate(BaseModel):
    request_type: Literal["loop", "stems"]
    artist_name: str = Field(..., min_length=1, max_length=255)
    song_title: str = Field(..., min_length=1, max_length=255)
    reference_link: HttpUrl | None = Field(default=None, max_length=2048)
    notes: str | None = Field(default=None, max_length=2_000)

    @field_validator("artist_name", "song_title", "reference_link", "notes", mode="before")
    @classmethod
    def strip_text(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class LoopRequestResponse(BaseModel):
    id: uuid.UUID
    request_type: Literal["loop", "stems"]
    artist_name: str
    song_title: str
    reference_link: str | None
    notes: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
