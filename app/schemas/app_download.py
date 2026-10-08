import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.purchase import PaymentProvider

AppOS = Literal["macos", "windows", "linux", "android", "ios"]

# Mobile builds are always handed out free, even for a paid app: payment only
# applies to the desktop platforms.
FREE_PLATFORMS = frozenset({"android", "ios"})
AppCurrency = Literal["NGN", "USD"]

# Where an installer may live. Anything else is refused at the admin endpoint
# so a bad value is caught when it is entered, not when a buyer redeems.
FILE_URL_SCHEMES = ("r2://", "s3://", "https://")


class AppDownloadRequestBody(BaseModel):
    email: EmailStr
    os: AppOS
    app_name: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "Name of an app published at /admin/apps. Omit to get the default "
            "Kida installer."
        ),
    )
    provider: PaymentProvider | None = Field(
        default=None,
        description=(
            "Payment gateway for a paid app. Defaults to the first configured "
            "gateway suited to the app's currency."
        ),
    )

    @field_validator("app_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


def _validate_file_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(FILE_URL_SCHEMES):
        raise ValueError("file URLs must start with r2://, s3:// or https://")
    if value.startswith(("r2://", "s3://")):
        bucket, _, key = value[5:].partition("/")
        if not bucket or not key:
            raise ValueError("file URLs must look like r2://<bucket>/<key> or s3://<bucket>/<key>")
    return value


def _validate_platforms(value: dict[str, str]) -> dict[str, str]:
    if not value:
        raise ValueError("at least one platform installer is required")
    return {os: _validate_file_url(url) for os, url in value.items()}


def _check_pricing(is_paid: bool, price: Decimal | None, currency: str | None) -> None:
    if is_paid and (price is None or currency is None):
        raise ValueError("price and currency are required for a paid app")


_PLATFORMS_DESCRIPTION = (
    "Installer location per OS, e.g. "
    '`{"macos": "r2://installers/toniq.dmg", "windows": "r2://installers/toniq.exe", '
    '"linux": "https://cdn.example.com/toniq.AppImage", "android": "r2://installers/toniq.apk", '
    '"ios": "https://apps.apple.com/app/id000000000"}`. Android and iOS are always free, '
    "even when the app is paid. "
    "r2://<bucket>/<key> and s3://<bucket>/<key> are private objects served through a "
    "short-lived presigned URL; https:// URLs are redirected to as-is."
)


class DesktopAppCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    platforms: dict[AppOS, str] = Field(description=_PLATFORMS_DESCRIPTION)
    is_paid: bool = False
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: AppCurrency | None = None
    is_active: bool = True

    _platforms = field_validator("platforms")(_validate_platforms)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @model_validator(mode="after")
    def _pricing(self) -> "DesktopAppCreate":
        _check_pricing(self.is_paid, self.price, self.currency)
        if not self.is_paid:
            self.price = None
            self.currency = None
        return self


class DesktopAppUpdate(BaseModel):
    """Partial update; the merged result is re-checked by the service.

    ``platforms`` is merged into the existing map: a URL adds or replaces that
    OS's installer, and ``null`` removes it.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    platforms: dict[AppOS, str | None] | None = None
    is_paid: bool | None = None
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: AppCurrency | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class DesktopAppPublic(BaseModel):
    """What anyone may see about a published app.

    Desktop installer locations are never exposed. Android and iOS are free,
    so their links are listed directly under ``links`` when the app has them.
    """

    id: uuid.UUID
    name: str
    available_os: list[str]
    #: The platforms that need payment: the desktop ones, when the app is paid.
    paid_os: list[str]
    #: Direct download links for the free mobile builds, keyed by OS.
    links: dict[str, str] = Field(default_factory=dict)
    is_paid: bool
    price: Decimal | None = None
    currency: str | None = None

    @classmethod
    def from_app(cls, app, links: dict[str, str] | None = None) -> "DesktopAppPublic":
        return cls(
            links=links or {},
            id=app.id, name=app.name, available_os=sorted(app.platforms or {}),
            paid_os=sorted(
                os for os in (app.platforms or {}) if app.is_paid and os not in FREE_PLATFORMS
            ),
            is_paid=app.is_paid, price=app.price, currency=app.currency,
        )


class DesktopAppAdmin(BaseModel):
    id: uuid.UUID
    name: str
    platforms: dict[str, str]
    is_paid: bool
    price: Decimal | None = None
    currency: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
