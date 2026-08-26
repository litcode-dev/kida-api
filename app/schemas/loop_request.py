import uuid
from datetime import datetime

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.utils.text_moderation import find_banned_term, find_slur

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

    @field_validator("notes", "reference_link")
    @classmethod
    def reject_offensive_text(cls, value):
        """An admin reads every one of these by hand — keep abuse out of that inbox.

        The offending fragment is named in the error: without it the submitter
        cannot tell which field to fix, and a filter nobody can act on just
        looks broken.
        """
        text = str(value) if value is not None else None
        found = find_banned_term(text)
        if found:
            raise ValueError(f"remove the offensive language ({found}) and try again")
        return value

    @field_validator("artist_name", "song_title")
    @classmethod
    def reject_hateful_text(cls, value):
        """Slurs only, on the two fields that name somebody else's record.

        These are a citation, not the submitter's own words: plenty of real
        tracks are called things nobody would put in a note, and refusing them
        rejects the request rather than the behaviour. A slur is different —
        no catalogue lookup needs one.
        """
        found = find_slur(value)
        if found:
            raise ValueError(f"remove the offensive language ({found}) and try again")
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
    admin_response: str | None
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
    """A move through the queue, optionally with something to say about it.

    ``admin_response`` is left unset to keep whatever the request already
    carries; sent as null or an empty string it clears the old text. The router
    reads ``model_fields_set`` to tell those two apart, which is why there is no
    sentinel default here.
    """

    status: LoopRequestStatus
    admin_response: str | None = Field(default=None, max_length=1_000)

    @field_validator("admin_response", mode="before")
    @classmethod
    def strip_response(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value
