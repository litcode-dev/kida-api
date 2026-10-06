import re

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.utils.text_moderation import find_banned_term

_WHITESPACE = re.compile(r"\s+")


class ContactMessageCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    subject: str | None = Field(default=None, max_length=200)
    message: str = Field(..., min_length=1, max_length=5_000)

    @field_validator("name", "subject", mode="before")
    @classmethod
    def single_line(cls, value):
        """Name and subject end up in the notification's Subject header, so
        any newline is collapsed — a CR/LF there would let a submitter add
        headers of their own."""
        if isinstance(value, str):
            value = _WHITESPACE.sub(" ", value).strip()
            return value or None
        return value

    @field_validator("message", mode="before")
    @classmethod
    def strip_message(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("subject", "message")
    @classmethod
    def reject_offensive_text(cls, value):
        """A person reads every one of these in the team inbox, as with loop
        requests — the offending fragment is named so the sender can fix it."""
        found = find_banned_term(value)
        if found:
            raise ValueError(f"remove the offensive language ({found}) and try again")
        return value
