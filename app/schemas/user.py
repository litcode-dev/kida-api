import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, field_validator
from app.models.user import UserRole


class UserRegister(BaseModel):
    email: EmailStr
    password: str
    full_name: str = Field(max_length=255)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    avatar_url: str | None = None
    created_at: datetime
    is_suspended: bool = False
    suspension_reason: str | None = None
    is_verified: bool = False

    model_config = {"from_attributes": True}


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str

    @field_validator("code")
    @classmethod
    def code_format(cls, v: str) -> str:
        v = v.strip()
        if not (v.isdigit() and len(v) == 6):
            raise ValueError("Code must be 6 digits")
        return v


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class SuspendRequest(BaseModel):
    reason: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    full_name: str
    role: UserRole
    avatar_url: str | None = None
    subscribed_to_newsletter: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str


class OAuthCallbackRequest(BaseModel):
    code: str
    state: str | None = None


class GoogleTokenRequest(BaseModel):
    access_token: str


class AppleTokenRequest(BaseModel):
    identity_token: str
    full_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)


class DeleteAccountRequest(BaseModel):
    refresh_token: str | None = None
    # Optional, and kept apart from everything else that is being erased — see
    # AccountDeletionReason. Nobody is made to explain themselves to leave.
    reason: str | None = Field(default=None, max_length=1_000)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value


class DeletionRequestCreate(BaseModel):
    """Start the out-of-app deletion route for an address."""

    email: EmailStr


class DeletionRequestConfirm(BaseModel):
    """Redeem the signed link mailed by :class:`DeletionRequestCreate`."""

    token: str
    reason: str | None = Field(default=None, max_length=1_000)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value
