# tests/routers/test_admin_analytics.py
import uuid
from decimal import Decimal
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from app.schemas.admin_analytics import (
    PlatformRevenueSummary,
    UserGrowthStats,
    TopContentItem,
    TopProducerItem,
)
from app.schemas.producer_analytics import TypeStats


def test_platform_revenue_summary_model_dump():
    s = PlatformRevenueSummary(
        total_earnings=Decimal("100.00"),
        total_sales=5,
        by_type={
            "loops": TypeStats(earnings=Decimal("60.00"), sales=3, downloads=10),
            "drones": TypeStats(earnings=Decimal("40.00"), sales=2, downloads=5),
            "drum_kits": TypeStats(earnings=Decimal("0"), sales=0, downloads=0),
        },
        by_provider={"flutterwave": Decimal("60.00"), "paystack": Decimal("40.00")},
    )
    d = s.model_dump()
    assert d["total_earnings"] == Decimal("100.00")
    assert d["by_type"]["loops"]["sales"] == 3
    assert d["by_provider"]["flutterwave"] == Decimal("60.00")


def test_user_growth_stats_model_dump():
    u = UserGrowthStats(total_users=100, new_users=5, by_role={"user": 90, "producer": 9, "admin": 1})
    d = u.model_dump()
    assert d["total_users"] == 100
    assert d["new_users"] == 5
    assert d["by_role"]["producer"] == 9


def test_top_content_item_model_dump():
    item = TopContentItem(
        id=uuid.uuid4(),
        title="Test Loop",
        content_type="loop",
        thumbnail_url="https://cdn.example.com/thumb.jpg",
        earnings=Decimal("50.00"),
        sales=3,
    )
    d = item.model_dump()
    assert d["content_type"] == "loop"
    assert d["earnings"] == Decimal("50.00")


def test_top_content_item_null_thumbnail():
    item = TopContentItem(
        id=uuid.uuid4(),
        title="No Thumb",
        content_type="drone",
        thumbnail_url=None,
        earnings=Decimal("0"),
        sales=0,
    )
    assert item.model_dump()["thumbnail_url"] is None


def test_top_producer_item_model_dump():
    p = TopProducerItem(
        id=uuid.uuid4(),
        full_name="Jane Doe",
        email="jane@example.com",
        total_earnings=Decimal("200.00"),
        total_sales=10,
    )
    d = p.model_dump()
    assert d["full_name"] == "Jane Doe"
    assert d["total_earnings"] == Decimal("200.00")
