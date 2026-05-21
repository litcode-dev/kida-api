import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, model_validator, Field
from app.models.drone_pad import MusicalKey


class DronePadCategoryCreate(BaseModel):
    name: str
    description: str | None = None


class DronePadCategoryResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DronePadCreate(BaseModel):
    title: str
    description: str | None = None
    key: MusicalKey
    price: Decimal | None = None
    is_free: bool = False
    category_id: uuid.UUID | None = None


class DronePadUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    price: Decimal | None = None
    is_free: bool | None = None
    category_id: uuid.UUID | None = None


class DronePadResponse(BaseModel):
    id: uuid.UUID
    drone_id: uuid.UUID
    title: str
    description: str | None = None
    key: MusicalKey
    duration: int
    price: Decimal | None = None
    is_free: bool
    store_product_id: str | None = None
    category_id: uuid.UUID | None = None
    category: DronePadCategoryResponse | None = None
    preview_s3_key: str | None = Field(default=None, exclude=True)
    thumbnail_s3_key: str | None = Field(default=None, exclude=True)
    preview_url: str | None = None
    thumbnail_url: str | None = None
    download_count: int
    created_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def build_urls(self) -> "DronePadResponse":
        from app.config import get_settings
        base = get_settings().s3_cloudfront_url.rstrip("/")
        if not self.preview_url and self.preview_s3_key:
            self.preview_url = f"{base}/{self.preview_s3_key}" if base else self.preview_s3_key
        if self.thumbnail_s3_key:
            self.thumbnail_url = f"{base}/{self.thumbnail_s3_key}" if base else self.thumbnail_s3_key
        return self


class DroneResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None = None
    thumbnail_url: str | None = None
    price: Decimal | None = None
    is_free: bool
    store_product_id: str | None = None
    category_id: uuid.UUID | None = None
    category: DronePadCategoryResponse | None = None
    download_count: int
    created_at: datetime
    pads: list[DronePadResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class DronePadFilter(BaseModel):
    key: MusicalKey | None = None
    is_free: bool | None = None
    category_id: uuid.UUID | None = None
    page: int = 1
    page_size: int = 50
