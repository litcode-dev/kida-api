import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.purchase import PaymentProvider

AppOS = Literal["macos", "windows"]
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
        raise ValueError("file_url must start with r2://, s3:// or https://")
    if value.startswith(("r2://", "s3://")):
        bucket, _, key = value[5:].partition("/")
        if not bucket or not key:
            raise ValueError("file_url must look like r2://<bucket>/<key> or s3://<bucket>/<key>")
    return value


def _check_pricing(is_paid: bool, price: Decimal | None, currency: str | None) -> None:
    if is_paid and (price is None or currency is None):
        raise ValueError("price and currency are required for a paid app")


class DesktopAppCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    os: AppOS
    file_url: str = Field(
        max_length=1024,
        description=(
            "Installer location: r2://<bucket>/<key> or s3://<bucket>/<key> for a "
            "private object (served via a short-lived presigned URL), or an https:// URL."
        ),
    )
    is_paid: bool = False
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: AppCurrency | None = None
    is_active: bool = True

    _file_url = field_validator("file_url")(_validate_file_url)

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
    """Partial update; the merged result is re-checked by the service."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    os: AppOS | None = None
    file_url: str | None = Field(default=None, max_length=1024)
    is_paid: bool | None = None
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: AppCurrency | None = None
    is_active: bool | None = None

    @field_validator("file_url")
    @classmethod
    def _file_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_file_url(value)

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
    """What anyone may see about a published app — never where the file is."""

    id: uuid.UUID
    name: str
    os: str
    is_paid: bool
    price: Decimal | None = None
    currency: str | None = None

    model_config = {"from_attributes": True}


class DesktopAppAdmin(DesktopAppPublic):
    file_url: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
