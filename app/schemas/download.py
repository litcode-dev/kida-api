import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, model_validator
from app.models.loop import Genre, TempoFeel


class DownloadedLoopItem(BaseModel):
    loop_id: uuid.UUID
    title: str
    slug: str
    genre: Genre
    bpm: int
    key: str | None = None
    duration: int
    tempo_feel: TempoFeel
    price: Decimal
    is_free: bool
    thumbnail_url: str | None = None
    thumbnail_s3_key: str | None = None
    last_downloaded_at: datetime
    times_downloaded: int
    like_count: int = 0

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def build_thumbnail_url(self) -> "DownloadedLoopItem":
        from app.config import get_settings
        if self.thumbnail_s3_key:
            base = get_settings().s3_cloudfront_url.rstrip("/")
            self.thumbnail_url = f"{base}/{self.thumbnail_s3_key}" if base else self.thumbnail_s3_key
        self.thumbnail_s3_key = None
        return self
