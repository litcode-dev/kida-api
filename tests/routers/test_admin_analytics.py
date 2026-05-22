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


# ── helpers shared across service tests ─────────────────────────────────────

from app.models.loop import Loop, Genre, TempoFeel
from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.drum_kit import DrumKit
from app.models.purchase import Purchase, PurchaseType
from app.models.download import Download
from app.models.user import User, UserRole
from app.services.auth_service import hash_password
from app.services.admin_analytics_service import get_platform_analytics
from app.schemas.producer_analytics import AnalyticsParams, AnalyticsPeriod


async def _make_user(db, role=UserRole.user):
    u = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Test User",
        role=role,
    )
    db.add(u)
    await db.commit()
    return u


async def _make_loop(db, producer_id):
    loop = Loop(
        id=uuid.uuid4(),
        title=f"Loop {uuid.uuid4().hex[:4]}",
        slug=f"loop-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        bpm=100,
        duration=8,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("4.99"),
        is_free=False,
        is_paid=True,
        created_by=producer_id,
    )
    db.add(loop)
    await db.commit()
    return loop


async def _make_drone(db, producer_id):
    drone = Drone(
        id=uuid.uuid4(),
        title=f"Drone {uuid.uuid4().hex[:4]}",
        price=Decimal("3.99"),
        is_free=False,
        created_by=producer_id,
    )
    db.add(drone)
    await db.commit()
    pad = DronePad(
        id=uuid.uuid4(),
        drone_id=drone.id,
        key=MusicalKey.C,
        status="ready",
        duration=0,
    )
    db.add(pad)
    await db.commit()
    return drone, pad


async def _make_kit(db, producer_id):
    kit = DrumKit(
        id=uuid.uuid4(),
        title=f"Kit {uuid.uuid4().hex[:4]}",
        slug=f"kit-{uuid.uuid4().hex[:8]}",
        price=Decimal("9.99"),
        is_free=False,
        download_count=0,
        created_by=producer_id,
    )
    db.add(kit)
    await db.commit()
    return kit


async def _make_purchase(db, user_id, *, loop_id=None, drone_pad_id=None, drum_kit_id=None,
                          amount="4.99", provider="flutterwave"):
    from app.models.purchase import PaymentProvider
    p = Purchase(
        id=uuid.uuid4(),
        user_id=user_id,
        loop_id=loop_id,
        drone_pad_id=drone_pad_id,
        drum_kit_id=drum_kit_id,
        amount_paid=Decimal(amount),
        purchase_type=PurchaseType.one_time,
        payment_reference=str(uuid.uuid4()),
        payment_provider=PaymentProvider(provider),
    )
    db.add(p)
    await db.commit()
    return p


async def _make_download(db, user_id, *, loop_id=None, drone_pad_id=None, drum_kit_id=None):
    d = Download(
        id=uuid.uuid4(),
        user_id=user_id,
        loop_id=loop_id,
        drone_pad_id=drone_pad_id,
        drum_kit_id=drum_kit_id,
        download_url="https://example.com/file",
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    db.add(d)
    await db.commit()
    return d


# ── service tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_service_empty_db_returns_zeros(db_session):
    params = AnalyticsParams()
    result = await get_platform_analytics(db_session, params)
    assert result["revenue"]["total_earnings"] == Decimal("0")
    assert result["revenue"]["total_sales"] == 0
    assert result["users"]["total_users"] == 0
    assert result["top_content"] == []
    assert result["top_producers"] == []


@pytest.mark.asyncio
async def test_service_revenue_by_type_loop(db_session):
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session)
    loop = await _make_loop(db_session, producer.id)
    await _make_purchase(db_session, buyer.id, loop_id=loop.id, amount="4.99")

    result = await get_platform_analytics(db_session, AnalyticsParams())

    assert result["revenue"]["total_earnings"] == Decimal("4.99")
    assert result["revenue"]["total_sales"] == 1
    assert result["revenue"]["by_type"]["loops"]["earnings"] == Decimal("4.99")
    assert result["revenue"]["by_type"]["loops"]["sales"] == 1
    assert result["revenue"]["by_type"]["drones"]["earnings"] == Decimal("0")
    assert result["revenue"]["by_type"]["drum_kits"]["earnings"] == Decimal("0")


@pytest.mark.asyncio
async def test_service_revenue_by_type_drone(db_session):
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session)
    _, pad = await _make_drone(db_session, producer.id)
    await _make_purchase(db_session, buyer.id, drone_pad_id=pad.id, amount="3.99")

    result = await get_platform_analytics(db_session, AnalyticsParams())

    assert result["revenue"]["by_type"]["drones"]["earnings"] == Decimal("3.99")
    assert result["revenue"]["by_type"]["loops"]["earnings"] == Decimal("0")


@pytest.mark.asyncio
async def test_service_revenue_by_provider(db_session):
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session)
    loop = await _make_loop(db_session, producer.id)
    kit = await _make_kit(db_session, producer.id)
    await _make_purchase(db_session, buyer.id, loop_id=loop.id, amount="5.00", provider="flutterwave")
    await _make_purchase(db_session, buyer.id, drum_kit_id=kit.id, amount="3.00", provider="paystack")

    result = await get_platform_analytics(db_session, AnalyticsParams())

    assert result["revenue"]["by_provider"]["flutterwave"] == Decimal("5.00")
    assert result["revenue"]["by_provider"]["paystack"] == Decimal("3.00")


@pytest.mark.asyncio
async def test_service_downloads_counted_by_type(db_session):
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session)
    loop = await _make_loop(db_session, producer.id)
    await _make_download(db_session, buyer.id, loop_id=loop.id)
    await _make_download(db_session, buyer.id, loop_id=loop.id)

    result = await get_platform_analytics(db_session, AnalyticsParams())

    assert result["revenue"]["by_type"]["loops"]["downloads"] == 2
    assert result["revenue"]["by_type"]["drones"]["downloads"] == 0


@pytest.mark.asyncio
async def test_service_user_counts(db_session):
    await _make_user(db_session, UserRole.user)
    await _make_user(db_session, UserRole.user)
    await _make_user(db_session, UserRole.producer)

    result = await get_platform_analytics(db_session, AnalyticsParams())

    assert result["users"]["total_users"] == 3
    assert result["users"]["by_role"]["user"] == 2
    assert result["users"]["by_role"]["producer"] == 1


@pytest.mark.asyncio
async def test_service_new_users_time_filtered(db_session):
    from sqlalchemy import update
    from datetime import timedelta
    old_user = await _make_user(db_session, UserRole.user)
    await db_session.execute(
        update(User).where(User.id == old_user.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(days=60))
    )
    await db_session.commit()
    new_user = await _make_user(db_session, UserRole.user)  # created now

    params = AnalyticsParams(period=AnalyticsPeriod.d30)
    result = await get_platform_analytics(db_session, params)

    assert result["users"]["total_users"] == 2   # total not filtered
    assert result["users"]["new_users"] == 1      # only the recent one


@pytest.mark.asyncio
async def test_service_top_content_sorted_by_earnings(db_session):
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session)
    loop_a = await _make_loop(db_session, producer.id)
    loop_b = await _make_loop(db_session, producer.id)
    await _make_purchase(db_session, buyer.id, loop_id=loop_a.id, amount="1.00")
    await _make_purchase(db_session, buyer.id, loop_id=loop_b.id, amount="10.00")

    result = await get_platform_analytics(db_session, AnalyticsParams())

    top = result["top_content"]
    assert len(top) >= 2
    ids = [str(item["id"]) for item in top]
    assert ids.index(str(loop_b.id)) < ids.index(str(loop_a.id))


@pytest.mark.asyncio
async def test_service_top_content_limited_to_10(db_session):
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session)
    for i in range(12):
        loop = await _make_loop(db_session, producer.id)
        await _make_purchase(db_session, buyer.id, loop_id=loop.id, amount=str(i + 1))

    result = await get_platform_analytics(db_session, AnalyticsParams())

    assert len(result["top_content"]) == 10


@pytest.mark.asyncio
async def test_service_top_producers_sorted_by_earnings(db_session):
    producer_a = await _make_user(db_session, UserRole.producer)
    producer_b = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session)
    loop_a = await _make_loop(db_session, producer_a.id)
    loop_b = await _make_loop(db_session, producer_b.id)
    await _make_purchase(db_session, buyer.id, loop_id=loop_a.id, amount="5.00")
    await _make_purchase(db_session, buyer.id, loop_id=loop_b.id, amount="50.00")

    result = await get_platform_analytics(db_session, AnalyticsParams())

    top = result["top_producers"]
    assert len(top) == 2
    assert str(top[0]["id"]) == str(producer_b.id)
    assert top[0]["total_earnings"] == Decimal("50.00")


@pytest.mark.asyncio
async def test_service_period_filter_excludes_old_purchases(db_session):
    from sqlalchemy import update
    from datetime import timedelta
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session)
    loop = await _make_loop(db_session, producer.id)
    p = await _make_purchase(db_session, buyer.id, loop_id=loop.id, amount="4.99")
    await db_session.execute(
        update(Purchase).where(Purchase.id == p.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(days=60))
    )
    await db_session.commit()

    result = await get_platform_analytics(db_session, AnalyticsParams(period=AnalyticsPeriod.d30))

    assert result["revenue"]["total_earnings"] == Decimal("0")
    assert result["revenue"]["total_sales"] == 0
    assert result["top_producers"] == []


@pytest.mark.asyncio
async def test_service_drone_earnings_aggregate_across_pads(db_session):
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session)
    # Drone with TWO pads
    drone = Drone(
        id=uuid.uuid4(), title="Multi-pad Drone",
        price=Decimal("3.99"), is_free=False, created_by=producer.id,
    )
    db_session.add(drone)
    await db_session.commit()
    pad_a = DronePad(id=uuid.uuid4(), drone_id=drone.id, key=MusicalKey.C, status="ready", duration=0)
    pad_b = DronePad(id=uuid.uuid4(), drone_id=drone.id, key=MusicalKey.D, status="ready", duration=0)
    db_session.add(pad_a)
    db_session.add(pad_b)
    await db_session.commit()
    # Purchase on pad_a only
    await _make_purchase(db_session, buyer.id, drone_pad_id=pad_a.id, amount="5.00")

    result = await get_platform_analytics(db_session, AnalyticsParams())

    top = result["top_content"]
    drone_items = [i for i in top if i["content_type"] == "drone"]
    assert len(drone_items) == 1
    assert drone_items[0]["earnings"] == Decimal("5.00")
    assert drone_items[0]["sales"] == 1


@pytest.mark.asyncio
async def test_service_top_content_excludes_zero_sale_items(db_session):
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session)
    sold_loop = await _make_loop(db_session, producer.id)
    unsold_loop = await _make_loop(db_session, producer.id)
    await _make_purchase(db_session, buyer.id, loop_id=sold_loop.id, amount="5.00")
    # unsold_loop has no purchases

    result = await get_platform_analytics(db_session, AnalyticsParams())

    top_ids = [str(i["id"]) for i in result["top_content"]]
    assert str(sold_loop.id) in top_ids
    assert str(unsold_loop.id) not in top_ids


# ── endpoint tests ────────────────────────────────────────────────────────────

from app.services.auth_service import create_access_token


@pytest.mark.asyncio
async def test_endpoint_requires_auth(client):
    resp = await client.get("/api/v1/admin/analytics")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_requires_admin_role(client, db_session):
    regular_user = await _make_user(db_session, UserRole.user)
    token = create_access_token(str(regular_user.id), "user")
    resp = await client.get(
        "/api/v1/admin/analytics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_returns_envelope_shape(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    token = create_access_token(str(admin.id), "admin")
    resp = await client.get(
        "/api/v1/admin/analytics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    data = body["data"]
    assert "period" in data
    assert "revenue" in data
    assert "users" in data
    assert "top_content" in data
    assert "top_producers" in data


@pytest.mark.asyncio
async def test_endpoint_revenue_keys_present(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    token = create_access_token(str(admin.id), "admin")
    resp = await client.get(
        "/api/v1/admin/analytics",
        headers={"Authorization": f"Bearer {token}"},
    )
    revenue = resp.json()["data"]["revenue"]
    assert "total_earnings" in revenue
    assert "total_sales" in revenue
    assert "by_type" in revenue
    assert "by_provider" in revenue
    for key in ("loops", "drones", "drum_kits"):
        assert key in revenue["by_type"]


@pytest.mark.asyncio
async def test_endpoint_period_param(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    token = create_access_token(str(admin.id), "admin")
    resp = await client.get(
        "/api/v1/admin/analytics?period=30d",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    period = resp.json()["data"]["period"]
    assert period["from"] is not None
    assert period["to"] is not None


@pytest.mark.asyncio
async def test_endpoint_invalid_period_returns_422(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    token = create_access_token(str(admin.id), "admin")
    resp = await client.get(
        "/api/v1/admin/analytics?period=999d",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_from_without_to_returns_422(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    token = create_access_token(str(admin.id), "admin")
    resp = await client.get(
        "/api/v1/admin/analytics?from_date=2026-01-01",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_top_content_has_required_fields(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session, UserRole.user)
    loop = await _make_loop(db_session, producer.id)
    await _make_purchase(db_session, buyer.id, loop_id=loop.id, amount="5.00")
    token = create_access_token(str(admin.id), "admin")
    resp = await client.get(
        "/api/v1/admin/analytics",
        headers={"Authorization": f"Bearer {token}"},
    )
    top = resp.json()["data"]["top_content"]
    assert len(top) >= 1
    item = next(i for i in top if str(i["id"]) == str(loop.id))
    assert item["content_type"] == "loop"
    assert item["sales"] == 1


@pytest.mark.asyncio
async def test_endpoint_top_producers_has_required_fields(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    producer = await _make_user(db_session, UserRole.producer)
    buyer = await _make_user(db_session, UserRole.user)
    loop = await _make_loop(db_session, producer.id)
    await _make_purchase(db_session, buyer.id, loop_id=loop.id, amount="5.00")
    token = create_access_token(str(admin.id), "admin")
    resp = await client.get(
        "/api/v1/admin/analytics",
        headers={"Authorization": f"Bearer {token}"},
    )
    producers = resp.json()["data"]["top_producers"]
    assert len(producers) == 1
    p = producers[0]
    assert "id" in p
    assert "full_name" in p
    assert "email" in p
    assert "total_earnings" in p
    assert "total_sales" in p
