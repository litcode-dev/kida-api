# app/schemas/admin_analytics.py
import uuid
from decimal import Decimal
from pydantic import BaseModel
from app.schemas.producer_analytics import TypeStats


class PlatformRevenueSummary(BaseModel):
    total_earnings: Decimal
    total_sales: int
    by_type: dict[str, TypeStats]
    by_provider: dict[str, Decimal]


class UserGrowthStats(BaseModel):
    total_users: int
    new_users: int
    by_role: dict[str, int]


class TopContentItem(BaseModel):
    id: uuid.UUID
    title: str
    content_type: str
    thumbnail_url: str | None
    earnings: Decimal
    sales: int


class TopProducerItem(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    total_earnings: Decimal
    total_sales: int
