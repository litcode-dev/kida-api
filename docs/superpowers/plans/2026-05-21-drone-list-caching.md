# Drone List Caching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cache `GET /api/v1/drones` responses in Redis with per-filter-combination keys, invalidating all list entries whenever an admin or producer creates, updates, or deletes a drone.

**Architecture:** Filter params are encoded into a stable cache key (`drone:list:{key}:{is_free}:{category_id}:{page}:{page_size}`). On any write operation in the admin router, `delete_pattern("drone:list:*")` wipes all permutations. The read path follows the exact same pattern as the existing categories cache. The write path wraps invalidation in try/except with a structlog warning, matching the existing admin category handlers.

**Tech Stack:** FastAPI, Redis (`redis.asyncio`), existing `app/services/cache_service.py`, `unittest.mock.patch` + `AsyncMock` for tests.

---

## File Map

| File | Change |
|---|---|
| `app/services/cache_service.py` | Add `TTL_DRONE_LIST = 300` |
| `app/routers/drones.py` | Cache read/write in `list_drones`; update existing test to not break |
| `app/routers/admin.py` | `delete_pattern("drone:list:*")` in 4 write handlers |
| `tests/routers/test_drones.py` | Update existing test + add 3 cache behaviour tests |
| `tests/routers/test_admin_drones.py` | New file — 4 invalidation tests |

---

## Task 1: Add `TTL_DRONE_LIST` constant

**Files:**
- Modify: `app/services/cache_service.py`
- Test: `tests/routers/test_drones.py`

- [ ] **Step 1: Write the failing test**

Add to the top of `tests/routers/test_drones.py` (after existing imports):

```python
from app.services import cache_service
```

Add this test function:

```python
def test_ttl_drone_list_is_defined():
    assert cache_service.TTL_DRONE_LIST == 300
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_drones.py::test_ttl_drone_list_is_defined -v
```

Expected: `FAILED` — `AttributeError: module 'app.services.cache_service' has no attribute 'TTL_DRONE_LIST'`

- [ ] **Step 3: Add the constant to `cache_service.py`**

In `app/services/cache_service.py`, add `TTL_DRONE_LIST` after the existing TTL constants (line 14):

```python
TTL_DRONE_LIST = 300          # 5 minutes — invalidated explicitly on writes
```

- [ ] **Step 4: Run test to confirm it passes**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_drones.py::test_ttl_drone_list_is_defined -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/services/cache_service.py tests/routers/test_drones.py
git commit -m "feat: add TTL_DRONE_LIST constant to cache_service"
```

---

## Task 2: Cache the `list_drones` response (read path)

**Files:**
- Modify: `app/routers/drones.py`
- Modify: `tests/routers/test_drones.py`

- [ ] **Step 1: Write three failing tests**

Add these tests to `tests/routers/test_drones.py` (after the `test_ttl_drone_list_is_defined` test). The `from unittest.mock import AsyncMock, patch` import is already at the top of the file.

```python
@pytest.mark.asyncio
async def test_list_drones_writes_cache_on_miss(client, db_session):
    user = await _create_user(db_session)
    await _create_drone(db_session, user.id, title="Cello Pad")

    with patch("app.routers.drones.cache_service.get", new=AsyncMock(return_value=None)) as mock_get, \
         patch("app.routers.drones.cache_service.set", new=AsyncMock()) as mock_set:
        resp = await client.get("/api/v1/drones")

    assert resp.status_code == 200
    mock_get.assert_awaited_once_with("drone:list:none:none:none:1:50")
    assert mock_set.await_count == 1
    args = mock_set.call_args[0]
    assert args[0] == "drone:list:none:none:none:1:50"
    assert args[2] == cache_service.TTL_DRONE_LIST


@pytest.mark.asyncio
async def test_list_drones_returns_cached_data_on_hit(client, db_session):
    cached = {"items": [{"id": "abc", "title": "Cached"}], "total": 1, "page": 1, "page_size": 50}

    with patch("app.routers.drones.cache_service.get", new=AsyncMock(return_value=cached)), \
         patch("app.routers.drones.cache_service.set", new=AsyncMock()) as mock_set:
        resp = await client.get("/api/v1/drones")

    assert resp.status_code == 200
    assert resp.json()["data"] == cached
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_drones_cache_key_encodes_filters(client, db_session):
    with patch("app.routers.drones.cache_service.get", new=AsyncMock(return_value=None)) as mock_get, \
         patch("app.routers.drones.cache_service.set", new=AsyncMock()):
        await client.get("/api/v1/drones?key=A&is_free=true&page=2&page_size=10")

    mock_get.assert_awaited_once_with("drone:list:A:True:none:2:10")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_drones.py::test_list_drones_writes_cache_on_miss tests/routers/test_drones.py::test_list_drones_returns_cached_data_on_hit tests/routers/test_drones.py::test_list_drones_cache_key_encodes_filters -v
```

Expected: all three `FAILED` — `AssertionError` on mock assertions because `list_drones` doesn't call `cache_service` yet.

- [ ] **Step 3: Update existing `test_list_drones_returns_parent_drones` to patch cache**

The existing test will break after Step 4 because `list_drones` will call `cache_service.get`, which tries to connect to Redis. Patch it now.

Replace the existing `test_list_drones_returns_parent_drones` function in `tests/routers/test_drones.py`:

```python
@pytest.mark.asyncio
async def test_list_drones_returns_parent_drones(client, db_session):
    user = await _create_user(db_session)
    pad = await _create_drone(
        db_session, user.id, title="Dark Piano Pad", key=MusicalKey.C
    )

    with patch("app.routers.drones.cache_service.get", new=AsyncMock(return_value=None)), \
         patch("app.routers.drones.cache_service.set", new=AsyncMock()):
        resp = await client.get("/api/v1/drones")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["id"] == str(pad.drone_id)
    assert data["items"][0]["title"] == "Dark Piano Pad"
    assert data["items"][0]["pads"][0]["id"] == str(pad.id)
```

- [ ] **Step 4: Implement caching in `list_drones`**

Replace the `list_drones` handler in `app/routers/drones.py` (lines 40–59):

```python
@router.get("")
async def list_drones(
    key: MusicalKey | None = None,
    is_free: bool | None = None,
    category_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
):
    cache_key = (
        f"drone:list:{key.value if key else 'none'}"
        f":{is_free if is_free is not None else 'none'}"
        f":{category_id or 'none'}"
        f":{page}:{page_size}"
    )
    cached = await cache_service.get(cache_key)
    if cached is not None:
        return success(cached)

    filters = DronePadFilter(
        key=key, is_free=is_free, category_id=category_id,
        page=page, page_size=page_size,
    )
    drones, total = await drone_service.list_drones(db, filters)
    data = {
        "items": [DroneResponse.model_validate(d).model_dump(mode="json") for d in drones],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    await cache_service.set(cache_key, data, cache_service.TTL_DRONE_LIST)
    return success(data)
```

- [ ] **Step 5: Run all drone tests to confirm they pass**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_drones.py -v
```

Expected: all tests `PASSED`

- [ ] **Step 6: Commit**

```bash
git add app/routers/drones.py tests/routers/test_drones.py
git commit -m "feat: cache GET /api/v1/drones responses in Redis"
```

---

## Task 3: Invalidate cache on admin writes

**Files:**
- Modify: `app/routers/admin.py`
- Create: `tests/routers/test_admin_drones.py`

- [ ] **Step 1: Create the test file with four failing tests**

Create `tests/routers/test_admin_drones.py`:

```python
import pytest
import uuid
import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


async def _create_user(db, role=UserRole.user):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Test",
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


async def _create_drone(db, user_id):
    drone = Drone(
        id=uuid.uuid4(),
        title="Test Drone",
        price=Decimal("0.00"),
        is_free=True,
        created_by=user_id,
    )
    db.add(drone)
    await db.flush()
    pad = DronePad(
        id=uuid.uuid4(),
        drone_id=drone.id,
        key=MusicalKey.C,
        duration=30,
        status="ready",
    )
    db.add(pad)
    await db.commit()
    pad.drone = drone
    return pad


def _fake_drone(user_id: uuid.UUID) -> Drone:
    drone = Drone(
        id=uuid.uuid4(),
        title="Fake Drone",
        price=Decimal("0.00"),
        is_free=True,
        created_by=user_id,
        download_count=0,
        created_at=datetime.datetime.utcnow(),
    )
    drone.pads = []
    drone.category = None
    return drone


@pytest.mark.asyncio
async def test_upload_drone_invalidates_list_cache(client, db_session):
    user = await _create_user(db_session, role=UserRole.producer)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    fake_drone = _fake_drone(user.id)

    with patch("app.routers.admin.drone_service.create_drone", new=AsyncMock(return_value=fake_drone)), \
         patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()) as mock_invalidate:
        resp = await client.post(
            "/api/v1/admin/drones",
            data={"title": "New Drone", "key": "C", "is_free": "true"},
            files={"file": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_invalidate.assert_awaited_once_with("drone:list:*")


@pytest.mark.asyncio
async def test_bulk_upload_drones_invalidates_list_cache(client, db_session):
    user = await _create_user(db_session, role=UserRole.producer)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    fake_drone = _fake_drone(user.id)

    with patch("app.routers.admin.drone_service.bulk_create_drones", new=AsyncMock(return_value=(fake_drone, []))), \
         patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()) as mock_invalidate:
        resp = await client.post(
            "/api/v1/admin/drones/bulk",
            data={"title": "Bulk Drone", "keys": "C", "is_free": "true"},
            files={"files": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_invalidate.assert_awaited_once_with("drone:list:*")


@pytest.mark.asyncio
async def test_update_drone_invalidates_list_cache(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    pad = await _create_drone(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    with patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()) as mock_invalidate:
        resp = await client.put(
            f"/api/v1/admin/drones/{pad.drone_id}",
            json={"title": "Updated Title"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_invalidate.assert_awaited_once_with("drone:list:*")


@pytest.mark.asyncio
async def test_delete_drone_invalidates_list_cache(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    pad = await _create_drone(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    with patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()) as mock_invalidate:
        resp = await client.delete(
            f"/api/v1/admin/drones/{pad.drone_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_invalidate.assert_awaited_once_with("drone:list:*")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_drones.py -v
```

Expected: all four `FAILED` — `AssertionError: Expected 'delete_pattern' to have been awaited once` because the admin handlers don't call `delete_pattern` yet.

- [ ] **Step 3: Add cache invalidation to `upload_drone` in `app/routers/admin.py`**

After `drone = await drone_service.create_drone(db, file, data, producer.id, thumbnail=thumbnail)` (line 302), add:

```python
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        import structlog as _structlog
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="upload_drone", error=str(e))
```

The full `upload_drone` handler body after the change:

```python
    drone = await drone_service.create_drone(db, file, data, producer.id, thumbnail=thumbnail)
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        import structlog as _structlog
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="upload_drone", error=str(e))
    from app.tasks.upload_tasks import process_drone_upload
    for pad in drone.pads:
        process_drone_upload.delay(str(pad.id))
    return success(DroneResponse.model_validate(drone).model_dump(), "Drone pad upload queued")
```

- [ ] **Step 4: Add cache invalidation to `bulk_upload_drones` in `app/routers/admin.py`**

After `drone, pads = await drone_service.bulk_create_drones(...)` (line 339–342), add:

```python
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        import structlog as _structlog
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="bulk_upload_drones", error=str(e))
```

The full `bulk_upload_drones` handler body after the change:

```python
    drone, pads = await drone_service.bulk_create_drones(
        db, files, validated_keys, title, price, is_free, category_id, producer.id,
        thumbnail=thumbnail, description=description
    )
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        import structlog as _structlog
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="bulk_upload_drones", error=str(e))
    from app.tasks.upload_tasks import process_drone_upload
    for pad in pads:
        process_drone_upload.delay(str(pad.id))
    return success(
        DroneResponse.model_validate(drone).model_dump(),
        f"{len(pads)} drone pad(s) upload queued",
    )
```

- [ ] **Step 5: Add cache invalidation to `update_drone` in `app/routers/admin.py`**

After `drone = await drone_service.update_drone(db, drone_id, body)` (line 395), add:

```python
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        import structlog as _structlog
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="update_drone", error=str(e))
```

The full `update_drone` handler body after the change:

```python
    drone = await drone_service.update_drone(db, drone_id, body)
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        import structlog as _structlog
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="update_drone", error=str(e))
    return success(DroneResponse.model_validate(drone).model_dump(), "Drone pad updated")
```

- [ ] **Step 6: Add cache invalidation to `delete_drone` in `app/routers/admin.py`**

After `await drone_service.delete_drone(db, drone_id)` (line 405), add:

```python
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        import structlog as _structlog
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="delete_drone", error=str(e))
```

The full `delete_drone` handler body after the change:

```python
    await drone_service.delete_drone(db, drone_id)
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        import structlog as _structlog
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="delete_drone", error=str(e))
    return success(message="Drone pad deleted")
```

- [ ] **Step 7: Run all admin drone tests to confirm they pass**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_drones.py -v
```

Expected: all four `PASSED`

- [ ] **Step 8: Run the full test suite to confirm no regressions**

```bash
source .venv/bin/activate && python -m pytest -v
```

Expected: all tests `PASSED`

- [ ] **Step 9: Commit**

```bash
git add app/routers/admin.py tests/routers/test_admin_drones.py
git commit -m "feat: invalidate drone list cache on admin writes"
```
