# tests/routers/test_producer_analytics.py
import pytest
from datetime import date
from pydantic import ValidationError
from app.schemas.producer_analytics import AnalyticsParams, AnalyticsPeriod


def test_default_params():
    p = AnalyticsParams()
    assert p.period == AnalyticsPeriod.all
    assert p.loops_page == 1
    assert p.page_size == 20


def test_custom_date_range():
    p = AnalyticsParams(from_date=date(2026, 1, 1), to_date=date(2026, 5, 1))
    from_dt, to_dt = p.resolve_window()
    assert from_dt.year == 2026
    assert from_dt.month == 1
    assert to_dt.month == 5


def test_period_7d_returns_window():
    p = AnalyticsParams(period=AnalyticsPeriod.d7)
    from_dt, to_dt = p.resolve_window()
    assert (to_dt - from_dt).days == 7


def test_period_all_returns_none():
    p = AnalyticsParams(period=AnalyticsPeriod.all)
    from_dt, to_dt = p.resolve_window()
    assert from_dt is None
    assert to_dt is None


def test_from_without_to_raises():
    with pytest.raises(ValidationError):
        AnalyticsParams(from_date=date(2026, 1, 1))


def test_from_after_to_raises():
    with pytest.raises(ValidationError):
        AnalyticsParams(from_date=date(2026, 6, 1), to_date=date(2026, 1, 1))
