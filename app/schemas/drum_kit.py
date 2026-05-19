import uuid
from datetime import datetime
from pydantic import BaseModel, field_validator

DOWNLOAD_EXPIRY_SECONDS = 900  # 15 minutes


class DrumKitCreate(BaseModel):
    title: str
    description: str | None = None
    tags: list[str] = []
    is_free: bool = True


class DrumKitCategoryCreate(BaseModel):
    name: str


class DrumSampleResponse(BaseModel):
    id: uuid.UUID
    label: str
    duration: int
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DrumKitCategoryResponse(BaseModel):
    id: uuid.UUID
    drum_kit_id: uuid.UUID
    name: str
    samples: list[DrumSampleResponse] = []
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
    store_product_id: str | None = None
    categories: list[DrumKitCategoryResponse] = []
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


class DrumKitCategoryDownloadItem(BaseModel):
    id: uuid.UUID
    name: str
    samples: list[DrumSampleDownloadItem]


class DrumKitDownloadResponse(BaseModel):
    kit_id: uuid.UUID
    title: str
    categories: list[DrumKitCategoryDownloadItem]
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
