import enum

from pydantic import BaseModel, EmailStr, Field, model_validator


class BroadcastAudience(str, enum.Enum):
    """Who a broadcast goes to.

    ``users`` and ``all`` never include an address that explicitly unsubscribed
    from the newsletter — see broadcast_service.resolve_recipients.
    """

    users = "users"              # registered accounts
    subscribers = "subscribers"  # active newsletter subscribers
    all = "all"                  # both, deduplicated


class BroadcastRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    # Short line above the body, e.g. "NEW DROP". Falls back to the subject.
    heading: str | None = Field(default=None, max_length=120)
    body: str = Field(min_length=1, max_length=5000)
    cta_label: str | None = Field(default=None, max_length=60)
    cta_url: str | None = Field(default=None, max_length=500)
    audience: BroadcastAudience = BroadcastAudience.all

    @model_validator(mode="after")
    def cta_needs_both_parts(self) -> "BroadcastRequest":
        if bool(self.cta_label) != bool(self.cta_url):
            raise ValueError("cta_label and cta_url must be provided together")
        return self


class BroadcastPreviewRequest(BroadcastRequest):
    """A broadcast sent to one address instead of the audience."""

    to: EmailStr
