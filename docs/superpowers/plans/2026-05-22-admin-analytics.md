# Admin Analytics Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `GET /admin/analytics` returning platform-wide revenue, user growth, top content, and top producers, with optional time-window filtering.

**Architecture:** New `app/schemas/admin_analytics.py` holds response models; new `app/services/admin_analytics_service.py` holds all DB queries; the existing `app/routers/admin.py` gets one new endpoint at the bottom. Follows the same separation as `producer_analytics_service` / `producer.py`.

**Tech Stack:** FastAPI, SQLAlchemy async, Pydantic v2, pytest-asyncio, httpx AsyncClient.

---

## File Map

| Action | Path |
|--------|------|
| Create | `app/schemas/admin_analytics.py` |
| Create | `app/services/admin_analytics_service.py` |
| Create | `tests/routers/test_admin_analytics.py` |
| Modify | `app/routers/admin.py` (add endpoint + imports at bottom) |

---

## Task 1: Schemas

**Files:**
- Create: `app/schemas/admin_analytics.py`
- Test: `tests/routers/test_admin_analytics.py` (schema section only)

- [ ] **Step 1: Write the failing schema tests**

Create `tests/routers/test_admin_analytics.py` with this content:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/litecode/Documents/Projects/Python/litmusic-api
source .venv/bin/activate && python -m pytest tests/routers/test_admin_analytics.py -v 2>&1 | head -30
```

Expected: `ImportError` — `app.schemas.admin_analytics` not found.

- [ ] **Step 3: Create `app/schemas/admin_analytics.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_analytics.py::test_platform_revenue_summary_model_dump tests/routers/test_admin_analytics.py::test_user_growth_stats_model_dump tests/routers/test_admin_analytics.py::test_top_content_item_model_dump tests/routers/test_admin_analytics.py::test_top_content_item_null_thumbnail tests/routers/test_admin_analytics.py::test_top_producer_item_model_dump -v
```

Expected: 5 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/admin_analytics.py tests/routers/test_admin_analytics.py
git commit -m "feat: add admin analytics schemas"
```

---

## Task 2: Service

**Files:**
- Create: `app/services/admin_analytics_service.py`
- Modify: `tests/routers/test_admin_analytics.py` (append service tests)

- [ ] **Step 1: Append failing service tests**

Append the following to `tests/routers/test_admin_analytics.py` (after the existing schema tests):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_analytics.py -k "service" -v 2>&1 | head -30
```

Expected: `ImportError` — `app.services.admin_analytics_service` not found.

- [ ] **Step 3: Create `app/services/admin_analytics_service.py`**

```python
# app/services/admin_analytics_service.py
import uuid
from decimal import Decimal
from datetime import datetime
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.loop import Loop
from app.models.drone_pad import Drone, DronePad
from app.models.drum_kit import DrumKit
from app.models.purchase import Purchase
from app.models.download import Download
from app.models.user import User
from app.schemas.producer_analytics import AnalyticsParams, TypeStats
from app.schemas.admin_analytics import (
    PlatformRevenueSummary,
    UserGrowthStats,
    TopContentItem,
    TopProducerItem,
)

_ZERO = Decimal("0")


async def _revenue_by_type(
    db: AsyncSession,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> tuple[dict[str, tuple[Decimal, int]], dict[str, int]]:
    pairs = [
        ("loops", Purchase.loop_id, Download.loop_id),
        ("drones", Purchase.drone_pad_id, Download.drone_pad_id),
        ("drum_kits", Purchase.drum_kit_id, Download.drum_kit_id),
    ]
    earnings_sales: dict[str, tuple[Decimal, int]] = {}
    downloads: dict[str, int] = {}

    for name, p_fk, d_fk in pairs:
        pq = select(
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        ).where(p_fk.isnot(None))
        if from_dt:
            pq = pq.where(Purchase.created_at >= from_dt)
        if to_dt:
            pq = pq.where(Purchase.created_at <= to_dt)
        row = (await db.execute(pq)).one()
        earnings_sales[name] = (row.earnings, row.sales)

        dq = select(func.count(Download.id).label("cnt")).where(d_fk.isnot(None))
        if from_dt:
            dq = dq.where(Download.downloaded_at >= from_dt)
        if to_dt:
            dq = dq.where(Download.downloaded_at <= to_dt)
        dl_row = (await db.execute(dq)).one()
        downloads[name] = dl_row.cnt

    return earnings_sales, downloads


async def _revenue_by_provider(
    db: AsyncSession,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> dict[str, Decimal]:
    pq = (
        select(
            Purchase.payment_provider,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
        )
        .where(Purchase.payment_provider.isnot(None))
        .group_by(Purchase.payment_provider)
    )
    if from_dt:
        pq = pq.where(Purchase.created_at >= from_dt)
    if to_dt:
        pq = pq.where(Purchase.created_at <= to_dt)
    rows = (await db.execute(pq)).all()
    return {r.payment_provider.value: r.earnings for r in rows}


async def _user_stats(
    db: AsyncSession,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> UserGrowthStats:
    total = await db.scalar(select(func.count()).select_from(User)) or 0

    new_q = select(func.count()).select_from(User)
    if from_dt:
        new_q = new_q.where(User.created_at >= from_dt)
    if to_dt:
        new_q = new_q.where(User.created_at <= to_dt)
    new_users = await db.scalar(new_q) or 0

    role_rows = (await db.execute(
        select(User.role, func.count(User.id).label("cnt")).group_by(User.role)
    )).all()
    by_role = {r.role.value: r.cnt for r in role_rows}

    return UserGrowthStats(total_users=total, new_users=new_users, by_role=by_role)


async def _top_content(
    db: AsyncSession,
    from_dt: datetime | None,
    to_dt: datetime | None,
    cf_base: str,
    limit: int = 10,
) -> list[TopContentItem]:
    items: list[TopContentItem] = []

    # Loops — outer join so items with zero sales are included
    loop_join = [Purchase.loop_id == Loop.id]
    if from_dt:
        loop_join.append(Purchase.created_at >= from_dt)
    if to_dt:
        loop_join.append(Purchase.created_at <= to_dt)
    lq = (
        select(
            Loop.id,
            Loop.title,
            Loop.thumbnail_s3_key,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .outerjoin(Purchase, and_(*loop_join))
        .group_by(Loop.id, Loop.title, Loop.thumbnail_s3_key)
    )
    for r in (await db.execute(lq)).all():
        items.append(TopContentItem(
            id=r.id,
            title=r.title,
            content_type="loop",
            thumbnail_url=f"{cf_base}/{r.thumbnail_s3_key}" if r.thumbnail_s3_key else None,
            earnings=r.earnings,
            sales=r.sales,
        ))

    # Drones — aggregate via DronePad
    drone_join = [Purchase.drone_pad_id == DronePad.id]
    if from_dt:
        drone_join.append(Purchase.created_at >= from_dt)
    if to_dt:
        drone_join.append(Purchase.created_at <= to_dt)
    dq = (
        select(
            Drone.id,
            Drone.title,
            Drone.thumbnail_url.label("thumb"),
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .outerjoin(DronePad, DronePad.drone_id == Drone.id)
        .outerjoin(Purchase, and_(*drone_join))
        .group_by(Drone.id, Drone.title, Drone.thumbnail_url)
    )
    for r in (await db.execute(dq)).all():
        items.append(TopContentItem(
            id=r.id,
            title=r.title,
            content_type="drone",
            thumbnail_url=f"{cf_base}/{r.thumb}" if r.thumb else None,
            earnings=r.earnings,
            sales=r.sales,
        ))

    # Drum kits
    kit_join = [Purchase.drum_kit_id == DrumKit.id]
    if from_dt:
        kit_join.append(Purchase.created_at >= from_dt)
    if to_dt:
        kit_join.append(Purchase.created_at <= to_dt)
    kq = (
        select(
            DrumKit.id,
            DrumKit.title,
            DrumKit.thumbnail_s3_key,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .outerjoin(Purchase, and_(*kit_join))
        .group_by(DrumKit.id, DrumKit.title, DrumKit.thumbnail_s3_key)
    )
    for r in (await db.execute(kq)).all():
        items.append(TopContentItem(
            id=r.id,
            title=r.title,
            content_type="drum_kit",
            thumbnail_url=f"{cf_base}/{r.thumbnail_s3_key}" if r.thumbnail_s3_key else None,
            earnings=r.earnings,
            sales=r.sales,
        ))

    items.sort(key=lambda x: x.earnings, reverse=True)
    return items[:limit]


async def _top_producers(
    db: AsyncSession,
    from_dt: datetime | None,
    to_dt: datetime | None,
    limit: int = 10,
) -> list[TopProducerItem]:
    totals: dict[uuid.UUID, tuple[Decimal, int]] = {}

    # Loop purchases → producer
    lq = (
        select(
            Loop.created_by,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .join(Purchase, Purchase.loop_id == Loop.id)
        .group_by(Loop.created_by)
    )
    if from_dt:
        lq = lq.where(Purchase.created_at >= from_dt)
    if to_dt:
        lq = lq.where(Purchase.created_at <= to_dt)
    for r in (await db.execute(lq)).all():
        e, s = totals.get(r.created_by, (_ZERO, 0))
        totals[r.created_by] = (e + r.earnings, s + r.sales)

    # Drone purchases → producer
    dq = (
        select(
            Drone.created_by,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .join(DronePad, DronePad.drone_id == Drone.id)
        .join(Purchase, Purchase.drone_pad_id == DronePad.id)
        .group_by(Drone.created_by)
    )
    if from_dt:
        dq = dq.where(Purchase.created_at >= from_dt)
    if to_dt:
        dq = dq.where(Purchase.created_at <= to_dt)
    for r in (await db.execute(dq)).all():
        e, s = totals.get(r.created_by, (_ZERO, 0))
        totals[r.created_by] = (e + r.earnings, s + r.sales)

    # Drum kit purchases → producer
    kq = (
        select(
            DrumKit.created_by,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .join(Purchase, Purchase.drum_kit_id == DrumKit.id)
        .group_by(DrumKit.created_by)
    )
    if from_dt:
        kq = kq.where(Purchase.created_at >= from_dt)
    if to_dt:
        kq = kq.where(Purchase.created_at <= to_dt)
    for r in (await db.execute(kq)).all():
        e, s = totals.get(r.created_by, (_ZERO, 0))
        totals[r.created_by] = (e + r.earnings, s + r.sales)

    if not totals:
        return []

    sorted_ids = sorted(totals.keys(), key=lambda pid: totals[pid][0], reverse=True)[:limit]
    users = (await db.scalars(select(User).where(User.id.in_(sorted_ids)))).all()
    user_map = {u.id: u for u in users}

    return [
        TopProducerItem(
            id=pid,
            full_name=user_map[pid].full_name,
            email=user_map[pid].email,
            total_earnings=totals[pid][0],
            total_sales=totals[pid][1],
        )
        for pid in sorted_ids
        if pid in user_map
    ]


async def get_platform_analytics(db: AsyncSession, params: AnalyticsParams) -> dict:
    from app.config import get_settings
    cf_base = get_settings().s3_cloudfront_url.rstrip("/")
    from_dt, to_dt = params.resolve_window()

    earnings_sales, dl_by_type = await _revenue_by_type(db, from_dt, to_dt)
    by_provider = await _revenue_by_provider(db, from_dt, to_dt)
    user_stats = await _user_stats(db, from_dt, to_dt)
    top_content = await _top_content(db, from_dt, to_dt, cf_base)
    top_producers = await _top_producers(db, from_dt, to_dt)

    revenue = PlatformRevenueSummary(
        total_earnings=sum(v[0] for v in earnings_sales.values()),
        total_sales=sum(v[1] for v in earnings_sales.values()),
        by_type={
            name: TypeStats(earnings=v[0], sales=v[1], downloads=dl_by_type.get(name, 0))
            for name, v in earnings_sales.items()
        },
        by_provider=by_provider,
    )

    if from_dt and to_dt:
        period_out = {"from": from_dt.date().isoformat(), "to": to_dt.date().isoformat()}
    else:
        period_out = {"from": None, "to": None}

    return {
        "period": period_out,
        "revenue": revenue.model_dump(),
        "users": user_stats.model_dump(),
        "top_content": [item.model_dump() for item in top_content],
        "top_producers": [item.model_dump() for item in top_producers],
    }
```

- [ ] **Step 4: Run service tests to verify they pass**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_analytics.py -k "service" -v
```

Expected: all 11 service tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/services/admin_analytics_service.py app/schemas/admin_analytics.py tests/routers/test_admin_analytics.py
git commit -m "feat: add admin analytics service"
```

---

## Task 3: Endpoint

**Files:**
- Modify: `app/routers/admin.py` (add imports + endpoint)
- Modify: `tests/routers/test_admin_analytics.py` (append endpoint tests)

- [ ] **Step 1: Append failing endpoint tests**

Append to `tests/routers/test_admin_analytics.py`:

```python
# ── endpoint tests ────────────────────────────────────────────────────────────

from app.services.auth_service import create_access_token


@pytest.mark.asyncio
async def test_endpoint_requires_auth(client):
    resp = await client.get("/api/v1/admin/analytics")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_requires_admin_role(client, db_session):
    regular_user = await _make_user(db_session, UserRole.user)
    token = create_access_token({"sub": str(regular_user.id)})
    resp = await client.get(
        "/api/v1/admin/analytics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_returns_envelope_shape(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    token = create_access_token({"sub": str(admin.id)})
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
    token = create_access_token({"sub": str(admin.id)})
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
    token = create_access_token({"sub": str(admin.id)})
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
    token = create_access_token({"sub": str(admin.id)})
    resp = await client.get(
        "/api/v1/admin/analytics?period=999d",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_from_without_to_returns_422(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    token = create_access_token({"sub": str(admin.id)})
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
    token = create_access_token({"sub": str(admin.id)})
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
    token = create_access_token({"sub": str(admin.id)})
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
```

- [ ] **Step 2: Run endpoint tests to verify they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_analytics.py -k "endpoint" -v 2>&1 | head -30
```

Expected: tests fail with 404 (endpoint not yet wired).

- [ ] **Step 3: Add the endpoint to `app/routers/admin.py`**

Add these imports at the top of `app/routers/admin.py` (after existing imports):

```python
from datetime import date
from app.schemas.producer_analytics import AnalyticsPeriod, AnalyticsParams
from app.services.admin_analytics_service import get_platform_analytics
```

Then append this endpoint at the very end of `app/routers/admin.py`:

```python
# --- Platform analytics ---

@router.get(
    "/analytics",
    summary="Platform analytics (admin)",
    description=(
        "Returns platform-wide revenue, user growth, top-selling content, and top producers "
        "for the specified time window. Defaults to all-time if no period is given."
    ),
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
        422: {"description": "Invalid period or mismatched date range"},
    },
)
async def platform_analytics(
    period: AnalyticsPeriod = Query(AnalyticsPeriod.all),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    from pydantic import ValidationError
    from app.exceptions import AppError
    try:
        params = AnalyticsParams(period=period, from_date=from_date, to_date=to_date)
    except ValidationError as e:
        err = e.errors()[0]
        msg = str(err.get("ctx", {}).get("error", err["msg"]))
        raise AppError(msg, status_code=422)
    data = await get_platform_analytics(db, params)
    return success(data)
```

- [ ] **Step 4: Run all admin analytics tests**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_analytics.py -v
```

Expected: all tests PASSED (schema + service + endpoint).

- [ ] **Step 5: Run the full test suite to check for regressions**

```bash
source .venv/bin/activate && python -m pytest --tb=short -q
```

Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin.py tests/routers/test_admin_analytics.py
git commit -m "feat: add GET /admin/analytics platform overview endpoint"
```
