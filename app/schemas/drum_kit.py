import uuid
from decimal import Decimal
from datetime import datetime
from pydantic import BaseModel, field_validator, model_validator

DOWNLOAD_EXPIRY_SECONDS = 900  # 15 minutes


class DrumKitCreate(BaseModel):
    title: str
    description: str | None = None
    tags: list[str] = []
    is_free: bool = True
    price: Decimal | None = None

    @model_validator(mode="after")
    def price_required_for_paid(self) -> "DrumKitCreate":
        if not self.is_free and self.price is None:
            raise ValueError("price is required for paid drum kits")
        if self.is_free:
            self.price = None
        return self


class DrumKitUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    price: Decimal | None = None
    is_free: bool | None = None

    @model_validator(mode="after")
    def price_required_for_paid(self) -> "DrumKitUpdate":
        if self.is_free is False and self.price is None:
            raise ValueError("price is required for paid drum kits")
        if self.is_free is True:
            self.price = None
        return self


class DrumSampleResponse(BaseModel):
    id: uuid.UUID
    label: str
    duration: int
    status: str
    preview_url: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DrumKitResponse(BaseModel):
    id: uuid.UUID
    title: str
    slug: str
    description: str | None
    thumbnail_url: str | None = None
    tags: list[str]
    is_free: bool
    price: Decimal | None = None
    store_product_id: str | None = None
    samples: list[DrumSampleResponse] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class DrumSampleDownloadItem(BaseModel):
    id: uuid.UUID
    label: str
    signed_url: str
    aes_key: str
    aes_iv: str
    duration: int
    expires_in_seconds: int = DOWNLOAD_EXPIRY_SECONDS


class DrumKitDownloadResponse(BaseModel):
    kit_id: uuid.UUID
    title: str
    samples: list[DrumSampleDownloadItem]
    expires_in_seconds: int = DOWNLOAD_EXPIRY_SECONDS


class DrumKitFilter(BaseModel):
    search: str | None = None
    is_free: bool | None = None
    tags: list[str] | None = None
    page: int = 1
    page_size: int = 20

    @field_validator("page_size")
    @classmethod
    def cap_page_size(cls, v: int) -> int:
        return min(v, 100)
