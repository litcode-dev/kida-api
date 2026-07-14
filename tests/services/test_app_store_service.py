from decimal import Decimal

import pytest

from app.exceptions import AppError
from app.services import app_store_service


def _point(pid: str, price: str) -> dict:
    return {"id": pid, "attributes": {"customerPrice": price}}


def test_snap_price_point_exact_match():
    points = [_point("p1", "0.99"), _point("p2", "1.99"), _point("p3", "2.99")]
    pid, price = app_store_service.snap_price_point(points, Decimal("1.99"))
    assert pid == "p2"
    assert price == Decimal("1.99")


def test_snap_price_point_nearest():
    points = [_point("p1", "0.99"), _point("p2", "1.99"), _point("p3", "4.99")]
    pid, price = app_store_service.snap_price_point(points, Decimal("2.40"))
    assert pid == "p2"  # 1.99 is closer to 2.40 than 4.99
    assert price == Decimal("1.99")


def test_snap_price_point_tie_prefers_cheaper():
    points = [_point("p1", "1.00"), _point("p2", "3.00")]
    pid, price = app_store_service.snap_price_point(points, Decimal("2.00"))
    assert pid == "p1"
    assert price == Decimal("1.00")


def test_snap_price_point_above_all_snaps_to_highest():
    points = [_point("p1", "0.99"), _point("p2", "9.99")]
    pid, price = app_store_service.snap_price_point(points, Decimal("100.00"))
    assert pid == "p2"
    assert price == Decimal("9.99")


def test_snap_price_point_empty_raises():
    with pytest.raises(AppError):
        app_store_service.snap_price_point([], Decimal("1.99"))
