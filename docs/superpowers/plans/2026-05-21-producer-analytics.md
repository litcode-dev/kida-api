# Producer Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `GET /api/v1/producer/analytics` so producers can see aggregate earnings/sales/downloads and per-item breakdowns for their loops, drones, and drum kits, filterable by time period.

**Architecture:** A new `producer` router delegates to a dedicated `producer_analytics_service` that runs six sequential DB queries (earnings + downloads per content type), merges results, and returns a single response. Schemas live in `app/schemas/producer_analytics.py`. One migration adds `drone_pad_id` to `downloads` so drone download counts are time-filterable.

**Tech Stack:** FastAPI, SQLAlchemy async (asyncpg), Pydantic v2, Alembic, pytest-asyncio.

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Create | `app/schemas/producer_analytics.py` | Request params + response models |
| Create | `app/services/producer_analytics_service.py` | All DB queries and result assembly |
| Create | `app/routers/producer.py` | Single endpoint, auth guard |
| Create | `alembic/versions/v1w03r82s9t4_add_drone_pad_id_to_downloads.py` | Migration |
| Create | `tests/routers/test_producer_analytics.py` | Integration tests |
| Modify | `app/models/download.py` | Add `drone_pad_id` column |
| Modify | `app/main.py` | Register producer router |

---

## Task 1: Migration — add `drone_pad_id` to `downloads`

**Files:**
- Create: `alembic/versions/v1w03r82s9t4_add_drone_pad_id_to_downloads.py`

- [ ] **Step 1: Create the migration file**

```python
# alembic/versions/v1w03r82s9t4_add_drone_pad_id_to_downloads.py
"""add drone_pad_id to downloads

Revision ID: v1w03r82s9t4
Revises: u0v92q71r8s3
Create Date: 2026-05-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "v1w03r82s9t4"
down_revision = "u0v92q71r8s3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "downloads",
        sa.Column("drone_pad_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "downloads_drone_pad_id_fkey",
        "downloads",
        "drone_pads",
        ["drone_pad_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("downloads_drone_pad_id_fkey", "downloads", type_="foreignkey")
    op.drop_column("downloads", "drone_pad_id")
```

- [ ] **Step 2: Commit**

```bash
git add alembic/versions/v1w03r82s9t4_add_drone_pad_id_to_downloads.py
git commit -m "feat: migration — add drone_pad_id to downloads"
```

---

## Task 2: Update `Download` model

**Files:**
- Modify: `app/models/download.py`

- [ ] **Step 1: Add `drone_pad_id` column**

Replace the contents of `app/models/download.py` with:

```python
import uuid
from datetime import datetime
from sqlalchemy import Text, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    loop_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("loops.id", ondelete="SET NULL"), nullable=True)
    stem_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stems.id"), nullable=True)
    drum_kit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("drum_kits.id"), nullable=True)
    drone_pad_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("drone_pads.id", ondelete="SET NULL"), nullable=True)
    download_url: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 2: Commit**

```bash
git add app/models/download.py
git commit -m "feat: add drone_pad_id to Download model"
```

---

## Task 3: Pydantic schemas

**Files:**
- Create: `app/schemas/producer_analytics.py`

- [ ] **Step 1: Write the failing test for schema validation**

Create `tests/routers/test_producer_analytics.py` with the schema tests only (router tests come later):

```python
# tests/routers/test_producer_analytics.py
import pytest
from datetime import date
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
    with pytest.raises(Exception):
        AnalyticsParams(from_date=date(2026, 1, 1))


def test_from_after_to_raises():
    with pytest.raises(Exception):
        AnalyticsParams(from_date=date(2026, 6, 1), to_date=date(2026, 1, 1))
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /path/to/litmusic-api && source .venv/bin/activate
pytest tests/routers/test_producer_analytics.py -v
```

Expected: `ImportError` — `app.schemas.producer_analytics` does not exist yet.

- [ ] **Step 3: Create the schema file**

```python
# app/schemas/producer_analytics.py
import uuid
from datetime import date, datetime, timedelta
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
                datetime.combine(self.from_date, datetime.min.time()),
                datetime.combine(self.to_date, datetime.max.time()),
            )
        delta_map = {"7d": 7, "30d": 30, "90d": 90}
        if self.period.value in delta_map:
            now = datetime.utcnow()
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
    id: uuid.UUID
    title: str
    thumbnail_url: str | None
    earnings: Decimal
    sales: int
    downloads: int


class AnalyticsSection(BaseModel):
    items: list[AnalyticsItem]
    total: int
    page: int
    page_size: int
```

- [ ] **Step 4: Run the schema tests — confirm they pass**

```bash
pytest tests/routers/test_producer_analytics.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/producer_analytics.py tests/routers/test_producer_analytics.py
git commit -m "feat: producer analytics schemas"
```

---

## Task 4: Analytics service

**Files:**
- Create: `app/services/producer_analytics_service.py`

- [ ] **Step 1: Add service-level tests to the test file**

Append to `tests/routers/test_producer_analytics.py`:

```python
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
        key=MusicalKey.C, status="ready",
    )
    db.add(pad)
    await db.commit()
    return drone, pad


async def _make_kit(db, producer_id):
    kit = DrumKit(
        id=uuid.uuid4(), title="Test Kit",
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
        update(P).where(P.id == p.id).values(created_at=datetime.utcnow() - timedelta(days=60))
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/routers/test_producer_analytics.py -k "test_analytics" -v
```

Expected: `ImportError` — `producer_analytics_service` does not exist yet.

- [ ] **Step 3: Create the service**

```python
# app/services/producer_analytics_service.py
import uuid
from decimal import Decimal
from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.loop import Loop
from app.models.drone_pad import Drone, DronePad
from app.models.drum_kit import DrumKit
from app.models.purchase import Purchase
from app.models.download import Download
from app.schemas.producer_analytics import (
    AnalyticsParams, AnalyticsItem, AnalyticsSection,
    AnalyticsSummary, TypeStats,
)

_ZERO = Decimal("0")


async def _loop_stats(
    db: AsyncSession,
    producer_id: uuid.UUID,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> tuple[dict, dict, list]:
    loops = list((await db.scalars(
        select(Loop).where(Loop.created_by == producer_id).order_by(Loop.created_at.desc())
    )).all())
    if not loops:
        return {}, {}, []
    loop_ids = [l.id for l in loops]

    eq = (
        select(
            Purchase.loop_id,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .where(Purchase.loop_id.in_(loop_ids))
        .group_by(Purchase.loop_id)
    )
    if from_dt:
        eq = eq.where(Purchase.created_at >= from_dt)
    if to_dt:
        eq = eq.where(Purchase.created_at <= to_dt)

    dq = (
        select(Download.loop_id, func.count(Download.id).label("downloads"))
        .where(Download.loop_id.in_(loop_ids))
        .group_by(Download.loop_id)
    )
    if from_dt:
        dq = dq.where(Download.downloaded_at >= from_dt)
    if to_dt:
        dq = dq.where(Download.downloaded_at <= to_dt)

    e_map = {r.loop_id: (r.earnings, r.sales) for r in (await db.execute(eq)).all()}
    d_map = {r.loop_id: r.downloads for r in (await db.execute(dq)).all()}
    return e_map, d_map, loops


async def _drone_stats(
    db: AsyncSession,
    producer_id: uuid.UUID,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> tuple[dict, dict, list]:
    drones = list((await db.scalars(
        select(Drone).where(Drone.created_by == producer_id).order_by(Drone.created_at.desc())
    )).all())
    if not drones:
        return {}, {}, []
    drone_ids = [d.id for d in drones]

    eq = (
        select(
            DronePad.drone_id,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .join(DronePad, Purchase.drone_pad_id == DronePad.id)
        .where(DronePad.drone_id.in_(drone_ids))
        .group_by(DronePad.drone_id)
    )
    if from_dt:
        eq = eq.where(Purchase.created_at >= from_dt)
    if to_dt:
        eq = eq.where(Purchase.created_at <= to_dt)

    dq = (
        select(DronePad.drone_id, func.count(Download.id).label("downloads"))
        .join(DronePad, Download.drone_pad_id == DronePad.id)
        .where(DronePad.drone_id.in_(drone_ids))
        .group_by(DronePad.drone_id)
    )
    if from_dt:
        dq = dq.where(Download.downloaded_at >= from_dt)
    if to_dt:
        dq = dq.where(Download.downloaded_at <= to_dt)

    e_map = {r.drone_id: (r.earnings, r.sales) for r in (await db.execute(eq)).all()}
    d_map = {r.drone_id: r.downloads for r in (await db.execute(dq)).all()}
    return e_map, d_map, drones


async def _kit_stats(
    db: AsyncSession,
    producer_id: uuid.UUID,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> tuple[dict, dict, list]:
    kits = list((await db.scalars(
        select(DrumKit).where(DrumKit.created_by == producer_id).order_by(DrumKit.created_at.desc())
    )).all())
    if not kits:
        return {}, {}, []
    kit_ids = [k.id for k in kits]

    eq = (
        select(
            Purchase.drum_kit_id,
            func.coalesce(func.sum(Purchase.amount_paid), _ZERO).label("earnings"),
            func.count(Purchase.id).label("sales"),
        )
        .where(Purchase.drum_kit_id.in_(kit_ids))
        .group_by(Purchase.drum_kit_id)
    )
    if from_dt:
        eq = eq.where(Purchase.created_at >= from_dt)
    if to_dt:
        eq = eq.where(Purchase.created_at <= to_dt)

    dq = (
        select(Download.drum_kit_id, func.count(Download.id).label("downloads"))
        .where(Download.drum_kit_id.in_(kit_ids))
        .group_by(Download.drum_kit_id)
    )
    if from_dt:
        dq = dq.where(Download.downloaded_at >= from_dt)
    if to_dt:
        dq = dq.where(Download.downloaded_at <= to_dt)

    e_map = {r.drum_kit_id: (r.earnings, r.sales) for r in (await db.execute(eq)).all()}
    d_map = {r.drum_kit_id: r.downloads for r in (await db.execute(dq)).all()}
    return e_map, d_map, kits


def _build_loop_section(loops, e_map, d_map, page, page_size, cf_base) -> AnalyticsSection:
    items = [
        AnalyticsItem(
            id=l.id,
            title=l.title,
            thumbnail_url=f"{cf_base}/{l.thumbnail_s3_key}" if l.thumbnail_s3_key else None,
            earnings=e_map.get(l.id, (_ZERO, 0))[0],
            sales=e_map.get(l.id, (_ZERO, 0))[1],
            downloads=d_map.get(l.id, 0),
        )
        for l in loops
    ]
    start = (page - 1) * page_size
    return AnalyticsSection(items=items[start:start + page_size], total=len(items), page=page, page_size=page_size)


def _build_drone_section(drones, e_map, d_map, page, page_size, cf_base) -> AnalyticsSection:
    # Drone.thumbnail_url stores the S3 key (set during migration from drone_pads.thumbnail_s3_key)
    items = [
        AnalyticsItem(
            id=d.id,
            title=d.title,
            thumbnail_url=f"{cf_base}/{d.thumbnail_url}" if d.thumbnail_url else None,
            earnings=e_map.get(d.id, (_ZERO, 0))[0],
            sales=e_map.get(d.id, (_ZERO, 0))[1],
            downloads=d_map.get(d.id, 0),
        )
        for d in drones
    ]
    start = (page - 1) * page_size
    return AnalyticsSection(items=items[start:start + page_size], total=len(items), page=page, page_size=page_size)


def _build_kit_section(kits, e_map, d_map, page, page_size, cf_base) -> AnalyticsSection:
    items = [
        AnalyticsItem(
            id=k.id,
            title=k.title,
            thumbnail_url=f"{cf_base}/{k.thumbnail_s3_key}" if k.thumbnail_s3_key else None,
            earnings=e_map.get(k.id, (_ZERO, 0))[0],
            sales=e_map.get(k.id, (_ZERO, 0))[1],
            downloads=d_map.get(k.id, 0),
        )
        for k in kits
    ]
    start = (page - 1) * page_size
    return AnalyticsSection(items=items[start:start + page_size], total=len(items), page=page, page_size=page_size)


async def get_producer_analytics(
    db: AsyncSession,
    producer_id: uuid.UUID,
    params: AnalyticsParams,
) -> dict:
    from app.config import get_settings
    cf_base = get_settings().s3_cloudfront_url.rstrip("/")
    from_dt, to_dt = params.resolve_window()

    loop_e, loop_d, loops = await _loop_stats(db, producer_id, from_dt, to_dt)
    drone_e, drone_d, drones = await _drone_stats(db, producer_id, from_dt, to_dt)
    kit_e, kit_d, kits = await _kit_stats(db, producer_id, from_dt, to_dt)

    loop_section = _build_loop_section(loops, loop_e, loop_d, params.loops_page, params.page_size, cf_base)
    drone_section = _build_drone_section(drones, drone_e, drone_d, params.drones_page, params.page_size, cf_base)
    kit_section = _build_kit_section(kits, kit_e, kit_d, params.drum_kits_page, params.page_size, cf_base)

    l_earn = sum(v[0] for v in loop_e.values())
    l_sales = sum(v[1] for v in loop_e.values())
    l_dl = sum(loop_d.values())

    dr_earn = sum(v[0] for v in drone_e.values())
    dr_sales = sum(v[1] for v in drone_e.values())
    dr_dl = sum(drone_d.values())

    k_earn = sum(v[0] for v in kit_e.values())
    k_sales = sum(v[1] for v in kit_e.values())
    k_dl = sum(kit_d.values())

    summary = AnalyticsSummary(
        total_earnings=l_earn + dr_earn + k_earn,
        total_sales=l_sales + dr_sales + k_sales,
        total_downloads=l_dl + dr_dl + k_dl,
        by_type={
            "loops": TypeStats(earnings=l_earn, sales=l_sales, downloads=l_dl),
            "drones": TypeStats(earnings=dr_earn, sales=dr_sales, downloads=dr_dl),
            "drum_kits": TypeStats(earnings=k_earn, sales=k_sales, downloads=k_dl),
        },
    )

    if from_dt and to_dt:
        period_out = {"from": from_dt.date().isoformat(), "to": to_dt.date().isoformat()}
    else:
        period_out = {"from": None, "to": None}

    return {
        "period": period_out,
        "summary": summary.model_dump(),
        "loops": loop_section.model_dump(),
        "drones": drone_section.model_dump(),
        "drum_kits": kit_section.model_dump(),
    }
```

- [ ] **Step 4: Run service tests — confirm they pass**

```bash
pytest tests/routers/test_producer_analytics.py -k "test_analytics" -v
```

Expected: all 5 service tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/producer_analytics_service.py tests/routers/test_producer_analytics.py
git commit -m "feat: producer analytics service"
```

---

## Task 5: Router + main.py registration

**Files:**
- Create: `app/routers/producer.py`
- Modify: `app/main.py`

- [ ] **Step 1: Write the failing router test**

Append to `tests/routers/test_producer_analytics.py`:

```python
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/routers/test_producer_analytics.py -k "endpoint" -v
```

Expected: `404` responses — the router does not exist yet.

- [ ] **Step 3: Create the router**

```python
# app/routers/producer.py
from datetime import date
from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.middleware.auth_middleware import require_producer
from app.services.producer_analytics_service import get_producer_analytics
from app.schemas.producer_analytics import AnalyticsParams, AnalyticsPeriod
from app.schemas.common import success
from app.exceptions import AppError

router = APIRouter(prefix="/producer", tags=["producer"])


@router.get("/analytics")
async def producer_analytics(
    period: AnalyticsPeriod = Query(AnalyticsPeriod.all),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    loops_page: int = Query(1),
    drones_page: int = Query(1),
    drum_kits_page: int = Query(1),
    page_size: int = Query(20),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    try:
        params = AnalyticsParams(
            period=period,
            from_date=from_date,
            to_date=to_date,
            loops_page=loops_page,
            drones_page=drones_page,
            drum_kits_page=drum_kits_page,
            page_size=page_size,
        )
    except ValidationError as e:
        raise AppError(e.errors()[0]["msg"], status_code=422)

    data = await get_producer_analytics(db, producer.id, params)
    return success(data)
```

- [ ] **Step 4: Register the router in `app/main.py`**

Add `producer` to the import line and `include_router` block:

```python
# Add to existing import line (around line 13):
from app.routers import auth, loops, stem_packs, payments, admin, downloads, likes, subscriptions, ai, drones, drum_kits, purchases, producer

# Add to _tags_metadata list (before the health entry):
{"name": "producer", "description": "Producer earnings and download analytics."},

# Add after the last include_router call (around line 120):
app.include_router(producer.router, prefix=PREFIX)
```

- [ ] **Step 5: Run router tests — confirm they pass**

```bash
pytest tests/routers/test_producer_analytics.py -k "endpoint" -v
```

Expected: all 5 router tests PASS.

- [ ] **Step 6: Run full test file**

```bash
pytest tests/routers/test_producer_analytics.py -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add app/routers/producer.py app/main.py tests/routers/test_producer_analytics.py
git commit -m "feat: producer analytics endpoint"
```

---

## Task 6: Final verification

- [ ] **Step 1: Run the full test suite**

```bash
pytest --tb=short -q
```

Expected: no new failures.

- [ ] **Step 2: Commit any fixes if needed, then tag completion**

```bash
git commit -m "feat: producer analytics — complete" --allow-empty
```
