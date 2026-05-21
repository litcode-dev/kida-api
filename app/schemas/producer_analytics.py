# app/schemas/producer_analytics.py
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pydantic import BaseModel, model_validator


class AnalyticsPeriod(str, Enum):
    d7 = "7d"
    d30 = "30d"
    d90 = "90d"
    all = "all"


class AnalyticsParams(BaseModel):
    period: AnalyticsPeriod = AnalyticsPeriod.all
    from_date: date | None = None
    to_date: date | None = None
    loops_page: int = 1
    drones_page: int = 1
    drum_kits_page: int = 1
    page_size: int = 20

    @model_validator(mode="after")
    def validate_dates(self) -> "AnalyticsParams":
        has_from = self.from_date is not None
        has_to = self.to_date is not None
        if has_from != has_to:
            raise ValueError("Both from_date and to_date must be provided together")
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date must be before to_date")
        return self

    def resolve_window(self) -> tuple[datetime | None, datetime | None]:
        if self.from_date and self.to_date:
            return (
                datetime.combine(self.from_date, datetime.min.time(), tzinfo=timezone.utc),
                datetime.combine(self.to_date, datetime.max.time(), tzinfo=timezone.utc),
            )
        delta_map = {"7d": 7, "30d": 30, "90d": 90}
        if self.period.value in delta_map:
            now = datetime.now(timezone.utc)
            return (now - timedelta(days=delta_map[self.period.value]), now)
        return (None, None)


class TypeStats(BaseModel):
    earnings: Decimal
    sales: int
    downloads: int


class AnalyticsSummary(BaseModel):
    total_earnings: Decimal
    total_sales: int
    total_downloads: int
    by_type: dict[str, TypeStats]


class AnalyticsItem(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    title: str
    thumbnail_url: str | None
    earnings: Decimal
    sales: int
    downloads: int


class AnalyticsSection(BaseModel):
    model_config = {"from_attributes": True}

    items: list[AnalyticsItem]
    total: int
    page: int
    page_size: int
