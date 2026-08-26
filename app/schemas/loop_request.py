import uuid
from datetime import datetime

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

LoopRequestType = Literal["loop", "stems"]
LoopRequestStatus = Literal["new", "in_progress", "fulfilled", "declined"]


class LoopRequestCreate(BaseModel):
    request_type: LoopRequestType
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
    """What the requester sees: their own submission and where it stands."""

    id: uuid.UUID
    request_type: LoopRequestType
    artist_name: str
    song_title: str
    reference_link: str | None
    notes: str | None
    status: LoopRequestStatus
    created_at: datetime

    model_config = {"from_attributes": True}


class AdminLoopRequestResponse(LoopRequestResponse):
    """The same request with who asked for it, for the team working the queue.

    ``requester_name`` and ``requester_email`` are filled in by the router from
    the joined user row, so they are not read off the LoopRequest itself.
    """

    user_id: uuid.UUID
    requester_name: str | None = None
    requester_email: str | None = None
    status_changed_at: datetime | None


class LoopRequestStatusUpdate(BaseModel):
    status: LoopRequestStatus
