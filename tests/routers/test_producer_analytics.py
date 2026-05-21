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


import pytest_asyncio
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.loop import Loop, Genre, TempoFeel
from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.drum_kit import DrumKit
from app.models.purchase import Purchase, PurchaseType
from app.models.download import Download
from app.models.user import User, UserRole
from app.services.auth_service import hash_password
from app.services.producer_analytics_service import get_producer_analytics
from app.schemas.producer_analytics import AnalyticsParams, AnalyticsPeriod
import uuid
from datetime import datetime, timezone


async def _make_producer(db):
    u = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("x"), full_name="Producer", role=UserRole.producer,
    )
    db.add(u)
    await db.commit()
    return u


async def _make_loop(db, producer_id):
    loop = Loop(
        id=uuid.uuid4(), title="Test Loop", slug=f"slug-{uuid.uuid4().hex[:6]}",
        genre=Genre.afrobeat, bpm=100, duration=8,
        tempo_feel=TempoFeel.mid, tags=[], price=Decimal("4.99"),
        is_free=False, is_paid=True, created_by=producer_id,
    )
    db.add(loop)
    await db.commit()
    return loop


async def _make_drone(db, producer_id):
    drone = Drone(
        id=uuid.uuid4(), title="Test Drone",
        price=Decimal("3.99"), is_free=False, created_by=producer_id,
    )
    db.add(drone)
    await db.commit()
    pad = DronePad(
        id=uuid.uuid4(), drone_id=drone.id,
        key=MusicalKey.C, status="ready", duration=0,
    )
    db.add(pad)
    await db.commit()
    return drone, pad


async def _make_kit(db, producer_id):
    kit = DrumKit(
        id=uuid.uuid4(), title="Test Kit",
        slug=f"kit-{uuid.uuid4().hex[:6]}",
        price=Decimal("9.99"), is_free=False,
        download_count=0, created_by=producer_id,
    )
    db.add(kit)
    await db.commit()
    return kit


async def _make_purchase(db, user_id, *, loop_id=None, drone_pad_id=None, drum_kit_id=None, amount="4.99"):
    p = Purchase(
        id=uuid.uuid4(), user_id=user_id,
        loop_id=loop_id, drone_pad_id=drone_pad_id, drum_kit_id=drum_kit_id,
        amount_paid=Decimal(amount),
        purchase_type=PurchaseType.one_time,
        payment_reference=str(uuid.uuid4()),
    )
    db.add(p)
    await db.commit()
    return p


async def _make_download(db, user_id, *, loop_id=None, drone_pad_id=None, drum_kit_id=None):
    d = Download(
        id=uuid.uuid4(), user_id=user_id,
        loop_id=loop_id, drone_pad_id=drone_pad_id, drum_kit_id=drum_kit_id,
        download_url="https://example.com/file",
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    db.add(d)
    await db.commit()
    return d


@pytest.mark.asyncio
async def test_analytics_empty_producer(db_session):
    producer = await _make_producer(db_session)
    params = AnalyticsParams()
    result = await get_producer_analytics(db_session, producer.id, params)
    assert result["summary"]["total_earnings"] == Decimal("0")
    assert result["summary"]["total_sales"] == 0
    assert result["summary"]["total_downloads"] == 0
    assert result["loops"]["total"] == 0
    assert result["drones"]["total"] == 0
    assert result["drum_kits"]["total"] == 0


@pytest.mark.asyncio
async def test_analytics_loop_earnings_and_downloads(db_session):
    producer = await _make_producer(db_session)
    buyer = await _make_producer(db_session)
    loop = await _make_loop(db_session, producer.id)
    await _make_purchase(db_session, buyer.id, loop_id=loop.id, amount="4.99")
    await _make_download(db_session, buyer.id, loop_id=loop.id)

    params = AnalyticsParams()
    result = await get_producer_analytics(db_session, producer.id, params)

    assert result["summary"]["total_earnings"] == Decimal("4.99")
    assert result["summary"]["total_sales"] == 1
    assert result["summary"]["total_downloads"] == 1
    assert result["loops"]["total"] == 1
    item = result["loops"]["items"][0]
    assert item["earnings"] == Decimal("4.99")
    assert item["sales"] == 1
    assert item["downloads"] == 1


@pytest.mark.asyncio
async def test_analytics_drone_aggregates_at_drone_level(db_session):
    producer = await _make_producer(db_session)
    buyer = await _make_producer(db_session)
    drone, pad = await _make_drone(db_session, producer.id)
    await _make_purchase(db_session, buyer.id, drone_pad_id=pad.id, amount="3.99")
    await _make_download(db_session, buyer.id, drone_pad_id=pad.id)

    params = AnalyticsParams()
    result = await get_producer_analytics(db_session, producer.id, params)

    assert result["summary"]["by_type"]["drones"]["earnings"] == Decimal("3.99")
    assert result["drones"]["total"] == 1
    item = result["drones"]["items"][0]
    assert str(item["id"]) == str(drone.id)


@pytest.mark.asyncio
async def test_analytics_period_filter_excludes_old_purchases(db_session):
    producer = await _make_producer(db_session)
    buyer = await _make_producer(db_session)
    loop = await _make_loop(db_session, producer.id)
    p = await _make_purchase(db_session, buyer.id, loop_id=loop.id, amount="4.99")
    # backdate the purchase to 60 days ago
    from sqlalchemy import update
    from app.models.purchase import Purchase as P
    from datetime import timedelta
    await db_session.execute(
        update(P).where(P.id == p.id).values(created_at=datetime.now(timezone.utc) - timedelta(days=60))
    )
    await db_session.commit()

    params = AnalyticsParams(period=AnalyticsPeriod.d30)
    result = await get_producer_analytics(db_session, producer.id, params)

    assert result["summary"]["total_earnings"] == Decimal("0")
    assert result["summary"]["total_sales"] == 0


@pytest.mark.asyncio
async def test_analytics_other_producers_content_excluded(db_session):
    producer = await _make_producer(db_session)
    other = await _make_producer(db_session)
    loop = await _make_loop(db_session, other.id)  # owned by OTHER
    await _make_purchase(db_session, producer.id, loop_id=loop.id, amount="4.99")

    params = AnalyticsParams()
    result = await get_producer_analytics(db_session, producer.id, params)
    assert result["loops"]["total"] == 0
    assert result["summary"]["total_earnings"] == Decimal("0")


from app.services.auth_service import create_access_token


@pytest.mark.asyncio
async def test_analytics_endpoint_requires_producer(client):
    resp = await client.get("/api/v1/producer/analytics")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_analytics_endpoint_returns_envelope(client, db_session):
    producer = await _make_producer(db_session)
    token = create_access_token({"sub": str(producer.id)})
    resp = await client.get(
        "/api/v1/producer/analytics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    data = body["data"]
    assert "summary" in data
    assert "loops" in data
    assert "drones" in data
    assert "drum_kits" in data
    assert "period" in data


@pytest.mark.asyncio
async def test_analytics_endpoint_period_param(client, db_session):
    producer = await _make_producer(db_session)
    token = create_access_token({"sub": str(producer.id)})
    resp = await client.get(
        "/api/v1/producer/analytics?period=30d",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["period"]["from"] is not None
    assert data["period"]["to"] is not None


@pytest.mark.asyncio
async def test_analytics_endpoint_invalid_period(client, db_session):
    producer = await _make_producer(db_session)
    token = create_access_token({"sub": str(producer.id)})
    resp = await client.get(
        "/api/v1/producer/analytics?period=999d",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_analytics_endpoint_from_without_to(client, db_session):
    producer = await _make_producer(db_session)
    token = create_access_token({"sub": str(producer.id)})
    resp = await client.get(
        "/api/v1/producer/analytics?from_date=2026-01-01",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
