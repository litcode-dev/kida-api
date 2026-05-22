# Producer/Admin Router Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move 18 producer-owned content endpoints from `app/routers/admin.py` to `app/routers/producer.py`, correcting 4 permission bugs in the process, so producers call `/producer/*` and admins-only operations stay at `/admin/*`.

**Architecture:** Each content group (loops+stem-packs, drones, drum-kits) is moved in one task: write routing tests for new URLs first, add endpoints to `producer.py`, confirm tests pass, remove from `admin.py`, confirm old URLs 404. A final cleanup task prunes now-unused imports from `admin.py`. No service layer or schema changes — only router code moves.

**Tech Stack:** FastAPI, SQLAlchemy async, pytest-asyncio, httpx AsyncClient.

---

## File Map

| Action | Path |
|--------|------|
| Modify | `app/routers/producer.py` — add 18 endpoints + expand imports |
| Modify | `app/routers/admin.py` — remove 18 endpoints + prune imports |
| Create | `tests/routers/test_producer_content.py` — routing + permission tests |

---

## Task 1: Loops and Stem-Packs

**Files:**
- Modify: `app/routers/producer.py`
- Modify: `app/routers/admin.py` (lines 67–211)
- Create/modify: `tests/routers/test_producer_content.py`

- [ ] **Step 1: Write failing routing tests**

Create `tests/routers/test_producer_content.py`:

```python
# tests/routers/test_producer_content.py
import pytest
import uuid
from app.models.user import User, UserRole
from app.services.auth_service import hash_password, create_access_token


async def _make_user(db, role=UserRole.user):
    u = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Test",
        role=role,
    )
    db.add(u)
    await db.commit()
    return u


# ── loops ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_producer_loops_route_exists(client):
    # Without auth, route must return 403 (not 404 — 404 means route is missing)
    resp = await client.post("/api/v1/producer/loops")
    assert resp.status_code != 404, "POST /producer/loops route is missing"


@pytest.mark.asyncio
async def test_admin_loops_removed(client):
    resp = await client.post("/api/v1/admin/loops")
    assert resp.status_code == 404, "POST /admin/loops should be gone"


@pytest.mark.asyncio
async def test_producer_loop_status_route_exists(client):
    fake_id = uuid.uuid4()
    resp = await client.get(f"/api/v1/producer/loops/{fake_id}/status")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_loop_status_removed(client):
    fake_id = uuid.uuid4()
    resp = await client.get(f"/api/v1/admin/loops/{fake_id}/status")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_loop_update_accepts_producer_token(client, db_session):
    producer = await _make_user(db_session, UserRole.producer)
    token = create_access_token({"sub": str(producer.id)})
    fake_id = uuid.uuid4()
    resp = await client.put(
        f"/api/v1/producer/loops/{fake_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    # 403 = wrong role; 404 = not found (correct — producer token accepted, loop just doesn't exist)
    assert resp.status_code != 403, "Producer should be allowed to call PUT /producer/loops/{id}"


@pytest.mark.asyncio
async def test_admin_loop_update_removed(client):
    fake_id = uuid.uuid4()
    resp = await client.put(f"/api/v1/admin/loops/{fake_id}")
    assert resp.status_code == 404


# ── stem-packs ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_producer_stem_packs_route_exists(client):
    resp = await client.post("/api/v1/producer/stem-packs")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_stem_packs_removed(client):
    resp = await client.post("/api/v1/admin/stem-packs")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_stem_pack_update_accepts_producer_token(client, db_session):
    producer = await _make_user(db_session, UserRole.producer)
    token = create_access_token({"sub": str(producer.id)})
    fake_id = uuid.uuid4()
    resp = await client.put(
        f"/api/v1/producer/stem-packs/{fake_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "test"},
    )
    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_admin_stem_pack_update_removed(client):
    fake_id = uuid.uuid4()
    resp = await client.put(f"/api/v1/admin/stem-packs/{fake_id}")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/litecode/Documents/Projects/Python/litmusic-api
source .venv/bin/activate && python -m pytest tests/routers/test_producer_content.py -v 2>&1 | head -30
```

Expected: `test_producer_loops_route_exists` fails with `assert 404 != 404` (route missing).

- [ ] **Step 3: Add loop + stem-pack endpoints to `app/routers/producer.py`**

First, update the imports at the top of `app/routers/producer.py` to:

```python
import uuid
from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import ValidationError
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.exceptions import AppError, NotFoundError
from app.middleware.auth_middleware import require_producer
from app.schemas.common import success
from app.schemas.drum_kit import DrumKitCreate, DrumKitFilter, DrumKitResponse, DrumKitUpdate
from app.schemas.drone_pad import (
    DronePadCategoryCreate,
    DronePadCategoryResponse,
    DronePadCreate,
    DronePadUpdate,
    DroneResponse,
)
from app.schemas.loop import LoopCreate, LoopUpdate, LoopResponse
from app.schemas.producer_analytics import AnalyticsParams, AnalyticsPeriod
from app.schemas.stem_pack import StemPackCreate, StemCreate, StemPackResponse, StemResponse
from app.models.drum_kit import DrumKit
from app.models.drone_pad import MusicalKey
from app.models.loop import Genre, TempoFeel
from app.models.user import User
from app.services import cache_service, drum_kit_service, drone_service, loop_service, stem_pack_service
from app.services.producer_analytics_service import get_producer_analytics
from app.tasks.notification_tasks import send_new_content_emails
from app.tasks.upload_tasks import process_drone_upload, process_drum_sample_upload, process_loop_upload
```

Then append these endpoints to `app/routers/producer.py` after the existing `producer_analytics` endpoint:

```python

# --- Loop endpoints ---

@router.post("/loops")
async def upload_loop(
    file: UploadFile = File(...),
    thumbnail: UploadFile | None = File(None),
    title: str = Form(...),
    genre: Genre = Form(...),
    bpm: int = Form(...),
    tempo_feel: TempoFeel = Form(...),
    price: Decimal = Form(...),
    is_free: bool = Form(False),
    tags: str = Form(""),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    data = LoopCreate(
        title=title, genre=genre, bpm=bpm,
        tempo_feel=tempo_feel, price=price, is_free=is_free,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    loop = await loop_service.create_loop(db, file, data, producer.id, thumbnail=thumbnail)
    process_loop_upload.delay(str(loop.id))
    send_new_content_emails.delay(loop.title, "loop")
    return success(LoopResponse.model_validate(loop).model_dump(), "Loop upload queued")


@router.get("/loops/{loop_id}/status")
async def loop_upload_status(
    loop_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    loop = await loop_service.get_loop(db, loop_id)
    return success({"id": str(loop.id), "status": loop.status})


@router.put("/loops/{loop_id}")
async def update_loop(
    loop_id: uuid.UUID,
    thumbnail: UploadFile | None = File(None),
    file: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    genre: Genre | None = Form(None),
    bpm: int | None = Form(None),
    tempo_feel: TempoFeel | None = Form(None),
    tags: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    data = LoopUpdate(
        title=title,
        description=description,
        genre=genre,
        bpm=bpm,
        tempo_feel=tempo_feel,
        tags=tags_list,
        price=price,
        is_free=is_free,
    )
    loop, should_reprocess = await loop_service.update_loop(
        db, loop_id, data, thumbnail=thumbnail, file=file
    )
    if should_reprocess:
        process_loop_upload.delay(str(loop_id))
    return success(LoopResponse.model_validate(loop).model_dump(), "Loop updated")


# --- StemPack endpoints ---

@router.post("/stem-packs")
async def create_stem_pack(
    body: StemPackCreate,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    pack = await stem_pack_service.create_stem_pack(db, body, producer.id)
    return success(StemPackResponse.model_validate(pack).model_dump(), "StemPack created")


@router.post("/stem-packs/{pack_id}/stems")
async def add_stem(
    pack_id: uuid.UUID,
    file: UploadFile = File(...),
    label: str = Form(...),
    duration: int = Form(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    data = StemCreate(label=label, duration=duration)
    stem = await stem_pack_service.add_stem_to_pack(db, pack_id, file, data)
    return success(StemResponse.model_validate(stem).model_dump(), "Stem added")


@router.put("/stem-packs/{pack_id}")
async def update_stem_pack(
    pack_id: uuid.UUID,
    body: StemPackCreate,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    from app.models.stem_pack import StemPack
    pack = await db.get(StemPack, pack_id)
    if not pack:
        raise NotFoundError("StemPack not found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(pack, field, value)
    await db.commit()
    await db.refresh(pack)
    return success(StemPackResponse.model_validate(pack).model_dump(), "StemPack updated")
```

- [ ] **Step 4: Run loop+stem-pack tests**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_producer_content.py -k "loop or stem" -v 2>&1
```

Expected: all loop and stem-pack tests PASS.

- [ ] **Step 5: Remove loop + stem-pack producer endpoints from `app/routers/admin.py`**

Delete these blocks from `admin.py` (keep the DELETE endpoints and the `# --- User management ---` section):

- The `# --- Loop endpoints ---` comment
- `upload_loop` function (POST /admin/loops)
- `loop_upload_status` function (GET /admin/loops/{loop_id}/status)
- `update_loop` function (PUT /admin/loops/{loop_id}) — **keep** `delete_loop`
- The `# --- StemPack endpoints ---` comment
- `create_stem_pack` function (POST /admin/stem-packs)
- `add_stem` function (POST /admin/stem-packs/{pack_id}/stems)
- `update_stem_pack` function (PUT /admin/stem-packs/{pack_id}) — **keep** `delete_stem_pack`

After removal, `admin.py` loops section should only contain `delete_loop`, and stem-packs section only `delete_stem_pack`.

- [ ] **Step 6: Verify old URLs are 404 and all tests still pass**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_producer_content.py -v 2>&1
```

Expected: all 10 tests PASS (including `test_admin_loops_removed`, `test_admin_stem_packs_removed`).

```bash
source .venv/bin/activate && python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: no regressions (same pass/fail counts as before this task).

- [ ] **Step 7: Commit**

```bash
git add app/routers/producer.py app/routers/admin.py tests/routers/test_producer_content.py
git commit -m "feat: move loop and stem-pack endpoints from admin to producer router"
```

---

## Task 2: Drone Endpoints

**Files:**
- Modify: `app/routers/producer.py` (append 8 drone endpoints)
- Modify: `app/routers/admin.py` (remove 8 drone endpoints, keep 2 deletes)
- Modify: `tests/routers/test_producer_content.py` (append drone tests)

- [ ] **Step 1: Append failing drone tests to `tests/routers/test_producer_content.py`**

Append to the test file:

```python

# ── drones ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_producer_drone_categories_post_exists(client):
    resp = await client.post("/api/v1/producer/drones/categories")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drone_categories_post_removed(client):
    resp = await client.post("/api/v1/admin/drones/categories")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_drone_categories_get_exists(client):
    resp = await client.get("/api/v1/producer/drones/categories")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drone_categories_get_removed(client):
    resp = await client.get("/api/v1/admin/drones/categories")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_drones_post_exists(client):
    resp = await client.post("/api/v1/producer/drones")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drones_post_removed(client):
    resp = await client.post("/api/v1/admin/drones")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_drones_bulk_exists(client):
    resp = await client.post("/api/v1/producer/drones/bulk")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drones_bulk_removed(client):
    resp = await client.post("/api/v1/admin/drones/bulk")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_drone_update_accepts_producer_token(client, db_session):
    producer = await _make_user(db_session, UserRole.producer)
    token = create_access_token({"sub": str(producer.id)})
    fake_id = uuid.uuid4()
    resp = await client.put(
        f"/api/v1/producer/drones/{fake_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code != 403, "Producer should be allowed on PUT /producer/drones/{id}"


@pytest.mark.asyncio
async def test_admin_drone_update_removed(client):
    fake_id = uuid.uuid4()
    resp = await client.put(f"/api/v1/admin/drones/{fake_id}")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run drone tests to confirm they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_producer_content.py -k "drone" -v 2>&1 | head -30
```

Expected: `test_producer_drone_categories_post_exists` fails with `assert 404 != 404`.

- [ ] **Step 3: Append drone endpoints to `app/routers/producer.py`**

Append after the stem-pack section:

```python

# --- Drone pad endpoints ---

@router.post("/drones/categories")
async def create_drone_category(
    body: DronePadCategoryCreate,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    import structlog as _structlog
    category = await drone_service.create_category(db, body, producer.id)
    data = DronePadCategoryResponse.model_validate(category).model_dump(mode="json")
    try:
        await cache_service.delete("drone:categories")
        await cache_service.set(f"drone:category:{category.id}", data, cache_service.TTL_DRONE_CATEGORIES)
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="create_drone_category", error=str(e))
    return success(data, "Category created")


@router.get("/drones/categories")
async def list_drone_categories(
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    categories = await drone_service.list_categories(db)
    return success([DronePadCategoryResponse.model_validate(c).model_dump() for c in categories])


@router.post("/drones")
async def upload_drone(
    file: UploadFile = File(...),
    thumbnail: UploadFile | None = File(None),
    title: str = Form(...),
    description: str | None = Form(None),
    key: MusicalKey = Form(...),
    price: Decimal | None = Form(None),
    is_free: bool = Form(False),
    category_id: uuid.UUID | None = Form(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    import structlog as _structlog
    if not is_free and price is None:
        raise AppError("price is required for paid drone pads", status_code=422)
    data = DronePadCreate(
        title=title,
        description=description,
        key=key,
        price=price,
        is_free=is_free,
        category_id=category_id,
    )
    drone = await drone_service.create_drone(db, file, data, producer.id, thumbnail=thumbnail)
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="upload_drone", error=str(e))
    for pad in drone.pads:
        process_drone_upload.delay(str(pad.id))
    send_new_content_emails.delay(drone.name, "drone_pad")
    return success(DroneResponse.model_validate(drone).model_dump(), "Drone pad upload queued")


@router.post("/drones/bulk")
async def bulk_upload_drones(
    files: list[UploadFile] = File(...),
    keys: str = Form(...),
    title: str = Form(...),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool = Form(False),
    category_id: uuid.UUID | None = Form(None),
    thumbnail: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    import structlog as _structlog
    if not is_free and price is None:
        raise AppError("price is required for paid drone pads", status_code=422)
    parsed_keys = [k.strip() for k in keys.split(",") if k.strip()]
    try:
        validated_keys = [MusicalKey(k) for k in parsed_keys]
    except ValueError as e:
        raise AppError(f"Invalid key value: {e}", status_code=422)
    if len(files) != len(validated_keys):
        raise AppError(
            f"Got {len(files)} file(s) but {len(validated_keys)} key(s); counts must match",
            status_code=422,
        )
    drone, pads = await drone_service.bulk_create_drones(
        db, files, validated_keys, title, price, is_free, category_id, producer.id,
        thumbnail=thumbnail, description=description
    )
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="bulk_upload_drones", error=str(e))
    for pad in pads:
        process_drone_upload.delay(str(pad.id))
    send_new_content_emails.delay(drone.name, "drone_pad")
    return success(
        DroneResponse.model_validate(drone).model_dump(),
        f"{len(pads)} drone pad(s) upload queued",
    )


@router.get("/drones/bulk/status")
async def bulk_drone_upload_status(
    ids: str,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    parsed_ids = [i.strip() for i in ids.split(",") if i.strip()]
    try:
        validated_ids = [uuid.UUID(i) for i in parsed_ids]
    except ValueError:
        raise AppError("Invalid UUID in ids", status_code=422)
    drones = await drone_service.get_drones_by_ids(db, validated_ids)
    return success([
        {"id": str(d.id), "drone_id": str(d.drone_id), "key": d.key, "status": d.status}
        for d in drones
    ])


@router.get("/drones/{drone_id}/status")
async def drone_upload_status(
    drone_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    drone = await drone_service.get_drone(db, drone_id)
    return success({
        "id": str(drone.id),
        "status": "ready" if all(p.status == "ready" for p in drone.pads) else "processing",
        "pads": [{"id": str(p.id), "key": p.key, "status": p.status} for p in drone.pads],
    })


@router.put("/drones/{drone_id}")
async def update_drone(
    drone_id: uuid.UUID,
    thumbnail: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    category_id: uuid.UUID | None = Form(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    import structlog as _structlog
    data = DronePadUpdate(
        title=title,
        description=description,
        price=price,
        is_free=is_free,
        category_id=category_id,
    )
    drone = await drone_service.update_drone(db, drone_id, data, thumbnail=thumbnail)
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="update_drone", error=str(e))
    return success(DroneResponse.model_validate(drone).model_dump(), "Drone updated")


@router.patch("/drones/{drone_id}/pads/{pad_id}")
async def replace_drone_pad_audio(
    drone_id: uuid.UUID,
    pad_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    import structlog as _structlog
    pad = await drone_service.replace_pad_audio(db, drone_id, pad_id, file)
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="replace_drone_pad_audio", error=str(e))
    process_drone_upload.delay(str(pad_id))
    return success({"pad_id": str(pad.id), "status": pad.status}, "Pad audio replacement queued")
```

- [ ] **Step 4: Run drone tests**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_producer_content.py -k "drone" -v 2>&1
```

Expected: all 10 drone tests PASS.

- [ ] **Step 5: Remove drone producer endpoints from `app/routers/admin.py`**

Delete these blocks from `admin.py` (the `# --- Drone pad administration ---` section), keeping only the two admin-only endpoints:

**Remove:**
- `create_drone_category` (POST /admin/drones/categories)
- `list_drone_categories` (GET /admin/drones/categories)
- `upload_drone` (POST /admin/drones)
- `bulk_upload_drones` (POST /admin/drones/bulk)
- `bulk_drone_upload_status` (GET /admin/drones/bulk/status)
- `drone_upload_status` (GET /admin/drones/{drone_id}/status)
- `update_drone` (PUT /admin/drones/{drone_id})
- `replace_drone_pad_audio` (PATCH /admin/drones/{drone_id}/pads/{pad_id})

**Keep** in `admin.py`:
- `delete_drone_category` (DELETE /admin/drones/categories/{category_id}) — `require_admin`
- `delete_drone` (DELETE /admin/drones/{drone_id}) — `require_admin`

- [ ] **Step 6: Verify and run full test suite**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_producer_content.py -v 2>&1 | tail -20
source .venv/bin/activate && python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: all producer_content tests pass, no regressions.

- [ ] **Step 7: Commit**

```bash
git add app/routers/producer.py app/routers/admin.py tests/routers/test_producer_content.py
git commit -m "feat: move drone endpoints from admin to producer router"
```

---

## Task 3: Drum-Kit Endpoints

**Files:**
- Modify: `app/routers/producer.py` (append 4 drum-kit endpoints)
- Modify: `app/routers/admin.py` (remove 3 drum-kit producer endpoints, keep DELETE)
- Modify: `tests/routers/test_producer_content.py` (append drum-kit tests)

- [ ] **Step 1: Append failing drum-kit tests**

Append to `tests/routers/test_producer_content.py`:

```python

# ── drum kits ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_producer_drum_kits_post_exists(client):
    resp = await client.post("/api/v1/producer/drum-kits")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drum_kits_post_removed(client):
    resp = await client.post("/api/v1/admin/drum-kits")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_drum_kits_get_exists(client):
    resp = await client.get("/api/v1/producer/drum-kits")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drum_kits_get_removed(client):
    resp = await client.get("/api/v1/admin/drum-kits")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_drum_kit_update_accepts_producer_token(client, db_session):
    producer = await _make_user(db_session, UserRole.producer)
    token = create_access_token({"sub": str(producer.id)})
    fake_id = uuid.uuid4()
    resp = await client.put(
        f"/api/v1/producer/drum-kits/{fake_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code != 403, "Producer should be allowed on PUT /producer/drum-kits/{id}"


@pytest.mark.asyncio
async def test_admin_drum_kit_update_removed(client):
    fake_id = uuid.uuid4()
    resp = await client.put(f"/api/v1/admin/drum-kits/{fake_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_drum_kit_sample_patch_exists(client):
    fake_kit = uuid.uuid4()
    fake_sample = uuid.uuid4()
    resp = await client.patch(f"/api/v1/producer/drum-kits/{fake_kit}/samples/{fake_sample}")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drum_kit_sample_patch_removed(client):
    fake_kit = uuid.uuid4()
    fake_sample = uuid.uuid4()
    resp = await client.patch(f"/api/v1/admin/drum-kits/{fake_kit}/samples/{fake_sample}")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run drum-kit tests to confirm they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_producer_content.py -k "drum" -v 2>&1 | head -20
```

Expected: `test_producer_drum_kits_post_exists` fails with 404.

- [ ] **Step 3: Append drum-kit endpoints to `app/routers/producer.py`**

Append after the drone section:

```python

# --- Drum kit endpoints ---

@router.post(
    "/drum-kits",
    summary="Create drum kit",
    description=(
        "Creates a drum kit and uploads all samples in one request. "
        "For paid kits (`is_free=false`), `price` is required — `store_product_id` is auto-generated. "
        "Samples are queued for background processing after upload; their `status` starts as `processing`."
    ),
    response_description="Created drum kit with samples",
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Producer or admin role required"},
        422: {"description": "Validation error — missing price for paid kit, or sample/label count mismatch"},
    },
    status_code=201,
)
async def create_drum_kit(
    thumbnail: UploadFile | None = File(None),
    title: str = Form(...),
    description: str | None = Form(None),
    tags: str = Form(""),
    is_free: bool = Form(True),
    price: Decimal | None = Form(None),
    sample_files: list[UploadFile] = File(...),
    sample_labels: str = Form(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    import structlog as _structlog
    labels = [l.strip() for l in sample_labels.split(",") if l.strip()]
    data = DrumKitCreate(
        title=title,
        description=description,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        is_free=is_free,
        price=price,
    )
    kit, sample_ids = await drum_kit_service.create_drum_kit(
        db, data, producer.id, sample_files, labels, thumbnail=thumbnail
    )
    for sid in sample_ids:
        process_drum_sample_upload.delay(sid)
    send_new_content_emails.delay(kit.title, "drum_kit")
    try:
        await cache_service.delete_pattern("drum_kit:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="create_drum_kit", error=str(e))
    return success(DrumKitResponse.model_validate(kit).model_dump(), "Drum kit created, samples queued for processing")


@router.get(
    "/drum-kits",
    summary="List drum kits (producer)",
    description="Paginated list of all drum kits with samples. Supports the same filters as the public endpoint.",
    response_description="Paginated drum kit list",
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Producer or admin role required"},
    },
)
async def list_drum_kits_producer(
    search: str | None = None,
    is_free: bool | None = None,
    tags: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    import asyncio as _asyncio
    from app.routers.drum_kits import _kit_to_dict
    filters = DrumKitFilter(
        search=search,
        is_free=is_free,
        tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None,
        page=page,
        page_size=page_size,
    )
    kits, total = await drum_kit_service.list_drum_kits(db, filters)
    return success({
        "items": list(await _asyncio.gather(*[_kit_to_dict(k) for k in kits])),
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.put("/drum-kits/{kit_id}")
async def update_drum_kit(
    kit_id: uuid.UUID,
    thumbnail: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    data = DrumKitUpdate(title=title, description=description, price=price, is_free=is_free)
    kit = await drum_kit_service.update_drum_kit(db, kit_id, data, thumbnail=thumbnail)
    return success(DrumKitResponse.model_validate(kit).model_dump(), "Drum kit updated")


@router.patch("/drum-kits/{kit_id}/samples/{sample_id}")
async def replace_drum_sample_audio(
    kit_id: uuid.UUID,
    sample_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    sample = await drum_kit_service.replace_sample_audio(db, kit_id, sample_id, file)
    process_drum_sample_upload.delay(str(sample_id))
    return success({"sample_id": str(sample.id), "status": sample.status}, "Sample audio replacement queued")
```

- [ ] **Step 4: Run drum-kit tests**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_producer_content.py -k "drum" -v 2>&1
```

Expected: all 8 drum-kit tests PASS.

- [ ] **Step 5: Remove drum-kit producer endpoints from `app/routers/admin.py`**

Delete from `admin.py` (the `# --- Drum kit endpoints ---` section), keeping only the DELETE:

**Remove:**
- `create_drum_kit` (POST /admin/drum-kits)
- `list_drum_kits_admin` (GET /admin/drum-kits)
- `update_drum_kit` (PUT /admin/drum-kits/{kit_id})
- `replace_drum_sample_audio` (PATCH /admin/drum-kits/{kit_id}/samples/{sample_id})

**Keep** in `admin.py`:
- `delete_drum_kit` (DELETE /admin/drum-kits/{kit_id}) — `require_admin`

- [ ] **Step 6: Verify full test suite**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_producer_content.py -v 2>&1 | tail -15
source .venv/bin/activate && python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: all 28 producer_content tests pass, no regressions.

- [ ] **Step 7: Commit**

```bash
git add app/routers/producer.py app/routers/admin.py tests/routers/test_producer_content.py
git commit -m "feat: move drum-kit endpoints from admin to producer router"
```

---

## Task 4: Clean Up admin.py Imports

**Files:**
- Modify: `app/routers/admin.py` (remove now-unused imports)

After the previous three tasks, `admin.py` no longer uses several imports that were needed only for the moved producer endpoints. This task removes them.

- [ ] **Step 1: Identify unused imports**

After the moves, `admin.py` no longer needs:
- `UploadFile, File, Form` — only needed for multipart upload endpoints (all moved)
- `loop_service, stem_pack_service, drone_service, drum_kit_service, cache_service` — all moved
- `LoopCreate, LoopUpdate, LoopResponse` — all moved
- `StemPackCreate, StemCreate, StemPackResponse, StemResponse` — all moved
- `DronePadCategoryCreate, DronePadCategoryResponse, DronePadCreate, DronePadUpdate, DroneResponse` — all moved
- `DrumKitCreate, DrumKitResponse, DrumKitUpdate` — only `DrumKit` model reference stays for delete
- `Genre, TempoFeel` — moved with loop endpoints
- `MusicalKey` — moved with drone endpoints
- `require_producer` — no remaining endpoints in admin.py use it
- `process_drone_upload, process_loop_upload, process_drum_sample_upload` — all moved
- `send_new_content_emails` — all moved
- `Decimal` — no remaining admin endpoints use it
- `AppError` — still used (keep it)
- `DrumKit` — check if delete_drum_kit uses it directly or via service

Read the remaining 12 endpoints in `admin.py` to confirm what's still needed, then update the import block to exactly what remains.

- [ ] **Step 2: Read the current admin.py imports and remaining endpoints**

Read `app/routers/admin.py` lines 1–35 (imports) and skim the remaining endpoint bodies to confirm which imports are actually used.

- [ ] **Step 3: Replace the import block in `app/routers/admin.py`**

Based on the 12 remaining admin-only endpoints, the import block should be reduced to:

```python
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.middleware.auth_middleware import require_admin
from app.schemas.user import UserResponse
from app.schemas.common import success
from app.models.user import User, UserRole
from app.exceptions import NotFoundError, AppError
import uuid
from datetime import date
from app.schemas.producer_analytics import AnalyticsPeriod, AnalyticsParams
from app.services.admin_analytics_service import get_platform_analytics
from app.services import cache_service
```

Note: `cache_service` is still needed for `delete_drum_kit` cache invalidation and drone/drone-category cache deletes in the remaining admin DELETE endpoints. Verify by reading the remaining endpoint bodies before removing it.

- [ ] **Step 4: Verify the app still imports cleanly**

```bash
source .venv/bin/activate && python -c "from app.routers.admin import router; print('OK')"
```

Expected: `OK` printed, no ImportError.

- [ ] **Step 5: Run full test suite**

```bash
source .venv/bin/activate && python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: same pass/fail as after Task 3.

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin.py
git commit -m "refactor: prune unused imports from admin router after producer split"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✅ 18 producer endpoints moved (loops ×3, stem-packs ×3, drones ×8, drum-kits ×4)
- ✅ 4 permission bugs corrected: PUT loops, PUT stem-packs, PUT drones, PUT drum-kits → `require_producer`
- ✅ 12 admin-only endpoints remain in `admin.py` with `require_admin`
- ✅ Old `/admin/*` URLs return 404
- ✅ No service/schema changes
- ✅ `main.py` unchanged

**No placeholders:** All code blocks are complete.

**Type consistency:** All schema imports match (LoopCreate/LoopUpdate/LoopResponse, StemPackCreate etc.) — same names as used in original admin.py code.
