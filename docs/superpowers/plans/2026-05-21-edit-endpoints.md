# Edit Endpoints (Drones, Loops, Drum Kits) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add/extend multipart edit endpoints for drones (metadata + thumbnail + per-pad audio), loops (metadata + thumbnail + audio), and drum kits (metadata + thumbnail + per-sample audio).

**Architecture:** All edit endpoints use `multipart/form-data` with all fields optional. Thumbnail replacement deletes the old S3 object and uploads a new one. Audio replacement deletes old processed S3 files, uploads a new raw WAV, resets status to "processing", and re-triggers the Celery processing task. The existing JSON `PUT /admin/drones/{id}` and `PUT /admin/loops/{id}` are converted to multipart in-place; `PUT /admin/drum-kits/{id}`, `PATCH /admin/drones/{id}/pads/{pad_id}`, and `PATCH /admin/drum-kits/{id}/samples/{sample_id}` are new.

**Tech Stack:** FastAPI (File/Form), SQLAlchemy async, boto3 via asyncio.to_thread, Celery, pytest-asyncio, unittest.mock

---

## File Map

| File | Change |
|---|---|
| `app/schemas/drone_pad.py` | Remove `key` and `thumbnail_url` from `DronePadUpdate` |
| `app/schemas/drum_kit.py` | Add `DrumKitUpdate` class |
| `app/services/drone_service.py` | Extend `update_drone` with thumbnail; add `replace_pad_audio` |
| `app/services/loop_service.py` | Extend `update_loop` with thumbnail and file |
| `app/services/drum_kit_service.py` | Add `update_drum_kit` and `replace_sample_audio` |
| `app/routers/admin.py` | Rewrite `update_drone` handler; rewrite `update_loop` handler; add `replace_drone_pad_audio`, `update_drum_kit`, `replace_drum_sample_audio` handlers; import `DrumKitUpdate` |
| `tests/routers/test_admin_edit.py` | New file — all 5 endpoint tests |

---

## Task 1: Drone edit — schema, service, handler

**Files:**
- Modify: `app/schemas/drone_pad.py`
- Modify: `app/services/drone_service.py`
- Modify: `app/routers/admin.py`
- Create: `tests/routers/test_admin_edit.py`

- [ ] **Step 1: Write failing tests**

Create `tests/routers/test_admin_edit.py`:

```python
import pytest
import uuid
import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.drum_kit import DrumKit, DrumSample
from app.models.loop import Loop, Genre, TempoFeel
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


async def _create_drone(db, user_id, title="Test Drone"):
    drone = Drone(
        id=uuid.uuid4(),
        title=title,
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


async def _create_loop(db, user_id):
    loop = Loop(
        id=uuid.uuid4(),
        title="Test Loop",
        slug=f"test-loop-{uuid.uuid4().hex[:8]}",
        genre=Genre.hiphop,
        bpm=90,
        duration=4,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("0.00"),
        is_free=True,
        is_paid=False,
        created_by=user_id,
    )
    db.add(loop)
    await db.commit()
    return loop


async def _create_drum_kit(db, user_id):
    kit_id = uuid.uuid4()
    kit = DrumKit(
        id=kit_id,
        title="Test Kit",
        slug=f"test-kit-{str(kit_id)[:8]}",
        is_free=True,
        tags=[],
        created_by=user_id,
    )
    db.add(kit)
    await db.flush()
    sample = DrumSample(
        id=uuid.uuid4(),
        drum_kit_id=kit.id,
        label="Kick",
        status="ready",
    )
    db.add(sample)
    await db.commit()
    return kit, sample


# --- Drone PUT tests ---

@pytest.mark.asyncio
async def test_update_drone_metadata_via_multipart(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    pad = await _create_drone(db_session, user.id, title="Old Title")
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    with patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()):
        resp = await client.put(
            f"/api/v1/admin/drones/{pad.drone_id}",
            data={"title": "New Title"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "New Title"


@pytest.mark.asyncio
async def test_update_drone_with_thumbnail_uploads_to_s3(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    pad = await _create_drone(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    with patch("app.services.drone_service.s3_service.upload_bytes", new=AsyncMock()) as mock_upload, \
         patch("app.services.drone_service.s3_service.delete_object", new=AsyncMock()), \
         patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()):
        resp = await client.put(
            f"/api/v1/admin/drones/{pad.drone_id}",
            files={"thumbnail": ("thumb.jpg", b"fake-image-bytes", "image/jpeg")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_upload.assert_awaited_once()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_edit.py::test_update_drone_metadata_via_multipart tests/routers/test_admin_edit.py::test_update_drone_with_thumbnail_uploads_to_s3 -v
```

Expected: both `FAILED` or `SKIPPED` (if test DB unavailable). If `FAILED`: error about endpoint expecting JSON body, not multipart. If `SKIPPED`: DB unavailable — that's fine, proceed.

- [ ] **Step 3: Remove `key` and `thumbnail_url` from `DronePadUpdate`**

In `app/schemas/drone_pad.py`, replace the `DronePadUpdate` class:

```python
class DronePadUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    price: Decimal | None = None
    is_free: bool | None = None
    category_id: uuid.UUID | None = None
```

- [ ] **Step 4: Extend `drone_service.update_drone` to handle optional thumbnail**

In `app/services/drone_service.py`, replace the `update_drone` function:

```python
async def update_drone(
    db: AsyncSession,
    drone_id: uuid.UUID,
    data: DronePadUpdate,
    thumbnail: UploadFile | None = None,
) -> Drone:
    drone = await get_drone(db, drone_id)

    if thumbnail:
        old_thumb_key = drone.pads[0].thumbnail_s3_key if drone.pads else None
        if old_thumb_key:
            await s3_service.delete_object(old_thumb_key)
        thumb_bytes = await thumbnail.read()
        content_type = thumbnail.content_type or "image/jpeg"
        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
        new_thumb_key = s3_service.s3_key_for_drone_thumbnail(str(drone_id), ext)
        await s3_service.upload_bytes(new_thumb_key, thumb_bytes, content_type)
        drone.thumbnail_url = _thumbnail_url_for_key(new_thumb_key)
        for pad in drone.pads:
            pad.thumbnail_s3_key = new_thumb_key

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(drone, field, value)

    await db.commit()
    return await get_drone(db, drone_id)
```

- [ ] **Step 5: Rewrite `update_drone` handler in `app/routers/admin.py` to multipart**

Replace the existing `update_drone` handler (currently at `@router.put("/drones/{drone_id}")`):

```python
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
    admin=Depends(require_admin),
):
    data = DronePadUpdate(
        title=title,
        description=description,
        price=price,
        is_free=is_free,
        category_id=category_id,
    )
    drone = await drone_service.update_drone(db, drone_id, data, thumbnail=thumbnail)
    import structlog as _structlog
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="update_drone", error=str(e))
    return success(DroneResponse.model_validate(drone).model_dump(), "Drone updated")
```

- [ ] **Step 6: Run tests to confirm they pass**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_edit.py::test_update_drone_metadata_via_multipart tests/routers/test_admin_edit.py::test_update_drone_with_thumbnail_uploads_to_s3 -v
```

Expected: both `PASSED` or `SKIPPED`. No `FAILED`.

- [ ] **Step 7: Commit**

```bash
git add app/schemas/drone_pad.py app/services/drone_service.py app/routers/admin.py tests/routers/test_admin_edit.py
git commit -m "feat: extend drone PUT to multipart with optional thumbnail"
```

---

## Task 2: Drone pad audio replacement

**Files:**
- Modify: `app/services/drone_service.py`
- Modify: `app/routers/admin.py`
- Modify: `tests/routers/test_admin_edit.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/routers/test_admin_edit.py`:

```python
# --- Drone PATCH /pads/{pad_id} tests ---

@pytest.mark.asyncio
async def test_replace_drone_pad_audio_sets_processing_and_queues_celery(client, db_session):
    user = await _create_user(db_session, role=UserRole.producer)
    pad = await _create_drone(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    mock_task = MagicMock()
    with patch("app.services.drone_service.validate_wav_upload", new=AsyncMock(return_value=b"fakewav")), \
         patch("app.services.drone_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.drone_service.s3_service.delete_object", new=AsyncMock()), \
         patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()), \
         patch("app.routers.admin.process_drone_upload", mock_task):
        resp = await client.patch(
            f"/api/v1/admin/drones/{pad.drone_id}/pads/{pad.id}",
            files={"file": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "processing"
    mock_task.delay.assert_called_once_with(str(pad.id))


@pytest.mark.asyncio
async def test_replace_drone_pad_audio_returns_404_if_pad_not_in_drone(client, db_session):
    user = await _create_user(db_session, role=UserRole.producer)
    pad = await _create_drone(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    wrong_drone_id = uuid.uuid4()

    with patch("app.services.drone_service.validate_wav_upload", new=AsyncMock(return_value=b"fakewav")), \
         patch("app.services.drone_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.drone_service.s3_service.delete_object", new=AsyncMock()):
        resp = await client.patch(
            f"/api/v1/admin/drones/{wrong_drone_id}/pads/{pad.id}",
            files={"file": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_edit.py::test_replace_drone_pad_audio_sets_processing_and_queues_celery tests/routers/test_admin_edit.py::test_replace_drone_pad_audio_returns_404_if_pad_not_in_drone -v
```

Expected: `FAILED` (404 on route not found) or `SKIPPED`.

- [ ] **Step 3: Add `replace_pad_audio` to `app/services/drone_service.py`**

Add after `update_drone`:

```python
async def replace_pad_audio(
    db: AsyncSession,
    drone_id: uuid.UUID,
    pad_id: uuid.UUID,
    file: UploadFile,
) -> DronePad:
    pad = await get_drone_pad(db, pad_id)
    if pad.drone_id != drone_id:
        raise NotFoundError(f"Pad {pad_id} not found in drone {drone_id}")

    wav_bytes = await validate_wav_upload(file)

    if pad.file_s3_key:
        await s3_service.delete_object(pad.file_s3_key)
    if pad.preview_s3_key:
        await s3_service.delete_object(pad.preview_s3_key)

    raw_key = s3_service.s3_key_for_raw_drone(str(pad_id))
    await s3_service.upload_bytes(raw_key, wav_bytes, "audio/wav")

    pad.file_s3_key = None
    pad.preview_s3_key = None
    pad.status = "processing"
    await db.commit()
    await db.refresh(pad)
    return pad
```

- [ ] **Step 4: Add `replace_drone_pad_audio` handler to `app/routers/admin.py`**

Add after the `update_drone` handler (before `delete_drone`). Also add the import for `process_drone_upload` at the top of the function scope:

```python
@router.patch("/drones/{drone_id}/pads/{pad_id}")
async def replace_drone_pad_audio(
    drone_id: uuid.UUID,
    pad_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    pad = await drone_service.replace_pad_audio(db, drone_id, pad_id, file)
    import structlog as _structlog
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="replace_drone_pad_audio", error=str(e))
    from app.tasks.upload_tasks import process_drone_upload
    process_drone_upload.delay(str(pad_id))
    return success({"pad_id": str(pad.id), "status": pad.status}, "Pad audio replacement queued")
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_edit.py::test_replace_drone_pad_audio_sets_processing_and_queues_celery tests/routers/test_admin_edit.py::test_replace_drone_pad_audio_returns_404_if_pad_not_in_drone -v
```

Expected: both `PASSED` or `SKIPPED`.

- [ ] **Step 6: Commit**

```bash
git add app/services/drone_service.py app/routers/admin.py tests/routers/test_admin_edit.py
git commit -m "feat: add PATCH /admin/drones/{id}/pads/{pad_id} for audio replacement"
```

---

## Task 3: Loop edit — service and handler

**Files:**
- Modify: `app/services/loop_service.py`
- Modify: `app/routers/admin.py`
- Modify: `tests/routers/test_admin_edit.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/routers/test_admin_edit.py`:

```python
# --- Loop PUT tests ---

@pytest.mark.asyncio
async def test_update_loop_metadata_via_multipart(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    loop = await _create_loop(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    resp = await client.put(
        f"/api/v1/admin/loops/{loop.id}",
        data={"title": "Updated Loop Title"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "Updated Loop Title"


@pytest.mark.asyncio
async def test_update_loop_with_file_sets_processing_and_queues_celery(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    loop = await _create_loop(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    mock_task = MagicMock()
    with patch("app.services.loop_service.validate_wav_upload", new=AsyncMock(return_value=b"fakewav")), \
         patch("app.services.loop_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.loop_service.s3_service.delete_object", new=AsyncMock()), \
         patch("app.routers.admin.process_loop_upload", mock_task):
        resp = await client.put(
            f"/api/v1/admin/loops/{loop.id}",
            files={"file": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_task.delay.assert_called_once_with(str(loop.id))


@pytest.mark.asyncio
async def test_update_loop_with_thumbnail_uploads_to_s3(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    loop = await _create_loop(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    with patch("app.services.loop_service.s3_service.upload_bytes", new=AsyncMock()) as mock_upload, \
         patch("app.services.loop_service.s3_service.delete_object", new=AsyncMock()):
        resp = await client.put(
            f"/api/v1/admin/loops/{loop.id}",
            files={"thumbnail": ("thumb.jpg", b"fake-image-bytes", "image/jpeg")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_upload.assert_awaited_once()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_edit.py::test_update_loop_metadata_via_multipart tests/routers/test_admin_edit.py::test_update_loop_with_file_sets_processing_and_queues_celery tests/routers/test_admin_edit.py::test_update_loop_with_thumbnail_uploads_to_s3 -v
```

Expected: `FAILED` or `SKIPPED`.

- [ ] **Step 3: Extend `loop_service.update_loop` to handle thumbnail and file**

In `app/services/loop_service.py`, replace the `update_loop` function. Also add the `validate_wav_upload` import at the top of the file if not already present:

```python
from app.utils.audio_validator import validate_wav_upload
```

Replace `update_loop`:

```python
async def update_loop(
    db: AsyncSession,
    loop_id: uuid.UUID,
    data: LoopUpdate,
    thumbnail: UploadFile | None = None,
    file: UploadFile | None = None,
) -> Loop:
    loop = await get_loop(db, loop_id)

    if thumbnail:
        if loop.thumbnail_s3_key:
            await s3_service.delete_object(loop.thumbnail_s3_key)
        thumb_bytes = await thumbnail.read()
        content_type = thumbnail.content_type or "image/jpeg"
        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
        new_thumb_key = s3_service.s3_key_for_loop_thumbnail(str(loop_id), ext)
        await s3_service.upload_bytes(new_thumb_key, thumb_bytes, content_type)
        loop.thumbnail_s3_key = new_thumb_key

    should_reprocess = False
    if file:
        wav_bytes = await validate_wav_upload(file)
        if loop.file_s3_key:
            await s3_service.delete_object(loop.file_s3_key)
        if loop.preview_s3_key:
            await s3_service.delete_object(loop.preview_s3_key)
        raw_key = s3_service.s3_key_for_raw_loop(str(loop_id))
        await s3_service.upload_bytes(raw_key, wav_bytes, "audio/wav")
        loop.file_s3_key = None
        loop.preview_s3_key = None
        loop.status = "processing"
        should_reprocess = True

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(loop, field, value)

    await db.commit()
    await db.refresh(loop)
    return loop, should_reprocess
```

Note: `update_loop` now returns a tuple `(loop, should_reprocess)`. The `should_reprocess` flag tells the router whether to trigger Celery.

- [ ] **Step 4: Rewrite `update_loop` handler in `app/routers/admin.py` to multipart**

Replace the existing `update_loop` handler (at `@router.put("/loops/{loop_id}")`):

```python
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
    admin=Depends(require_admin),
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
        from app.tasks.upload_tasks import process_loop_upload
        process_loop_upload.delay(str(loop_id))
    return success(LoopResponse.model_validate(loop).model_dump(), "Loop updated")
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_edit.py::test_update_loop_metadata_via_multipart tests/routers/test_admin_edit.py::test_update_loop_with_file_sets_processing_and_queues_celery tests/routers/test_admin_edit.py::test_update_loop_with_thumbnail_uploads_to_s3 -v
```

Expected: all `PASSED` or `SKIPPED`.

- [ ] **Step 6: Commit**

```bash
git add app/services/loop_service.py app/routers/admin.py tests/routers/test_admin_edit.py
git commit -m "feat: extend loop PUT to multipart with optional thumbnail and audio replacement"
```

---

## Task 4: Drum kit edit — schema, service, handler

**Files:**
- Modify: `app/schemas/drum_kit.py`
- Modify: `app/services/drum_kit_service.py`
- Modify: `app/routers/admin.py`
- Modify: `tests/routers/test_admin_edit.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/routers/test_admin_edit.py`:

```python
# --- Drum kit PUT tests ---

@pytest.mark.asyncio
async def test_update_drum_kit_metadata(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    kit, _ = await _create_drum_kit(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    resp = await client.put(
        f"/api/v1/admin/drum-kits/{kit.id}",
        data={"title": "Updated Kit"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "Updated Kit"


@pytest.mark.asyncio
async def test_update_drum_kit_with_thumbnail_uploads_to_s3(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    kit, _ = await _create_drum_kit(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    with patch("app.services.drum_kit_service.s3_service.upload_bytes", new=AsyncMock()) as mock_upload, \
         patch("app.services.drum_kit_service.s3_service.delete_object", new=AsyncMock()):
        resp = await client.put(
            f"/api/v1/admin/drum-kits/{kit.id}",
            files={"thumbnail": ("thumb.jpg", b"fake-image-bytes", "image/jpeg")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_upload.assert_awaited_once()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_edit.py::test_update_drum_kit_metadata tests/routers/test_admin_edit.py::test_update_drum_kit_with_thumbnail_uploads_to_s3 -v
```

Expected: `FAILED` (route not found) or `SKIPPED`.

- [ ] **Step 3: Add `DrumKitUpdate` to `app/schemas/drum_kit.py`**

Add after the `DrumKitCreate` class in `app/schemas/drum_kit.py`:

```python
class DrumKitUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    price: Decimal | None = None
    is_free: bool | None = None

    @model_validator(mode="after")
    def price_required_for_paid(self) -> "DrumKitUpdate":
        if self.is_free is False and self.price is None:
            raise ValueError("price is required for paid drum kits")
        if self.is_free is True:
            self.price = None
        return self
```

Ensure `model_validator` is already imported at the top of `app/schemas/drum_kit.py`. If not, add it to the pydantic import line.

- [ ] **Step 4: Add `update_drum_kit` to `app/services/drum_kit_service.py`**

Add after the `get_drum_kit` function:

```python
async def update_drum_kit(
    db: AsyncSession,
    kit_id: uuid.UUID,
    data: "DrumKitUpdate",
    thumbnail: UploadFile | None = None,
) -> DrumKit:
    from app.schemas.drum_kit import DrumKitUpdate
    kit = await get_drum_kit(db, kit_id)

    if thumbnail:
        if kit.thumbnail_s3_key:
            await s3_service.delete_object(kit.thumbnail_s3_key)
        thumb_bytes = await thumbnail.read()
        content_type = thumbnail.content_type or "image/jpeg"
        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
        new_thumb_key = s3_service.s3_key_for_drum_kit_thumbnail(str(kit_id), ext)
        await s3_service.upload_bytes(new_thumb_key, thumb_bytes, content_type)
        kit.thumbnail_s3_key = new_thumb_key

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(kit, field, value)

    await db.commit()
    await db.refresh(kit)
    return kit
```

- [ ] **Step 5: Add `update_drum_kit` handler to `app/routers/admin.py`**

First, add `DrumKitUpdate` to the existing drum kit schema import line in `app/routers/admin.py`:

```python
from app.schemas.drum_kit import DrumKitCreate, DrumKitResponse, DrumKitUpdate
```

Then add the handler near the other drum kit admin endpoints (after `DELETE /drum-kits/{kit_id}`):

```python
@router.put("/drum-kits/{kit_id}")
async def update_drum_kit(
    kit_id: uuid.UUID,
    thumbnail: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    from app.exceptions import AppError
    data = DrumKitUpdate(title=title, description=description, price=price, is_free=is_free)
    kit = await drum_kit_service.update_drum_kit(db, kit_id, data, thumbnail=thumbnail)
    return success(DrumKitResponse.model_validate(kit).model_dump(), "Drum kit updated")
```

- [ ] **Step 6: Run tests to confirm they pass**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_edit.py::test_update_drum_kit_metadata tests/routers/test_admin_edit.py::test_update_drum_kit_with_thumbnail_uploads_to_s3 -v
```

Expected: both `PASSED` or `SKIPPED`.

- [ ] **Step 7: Commit**

```bash
git add app/schemas/drum_kit.py app/services/drum_kit_service.py app/routers/admin.py tests/routers/test_admin_edit.py
git commit -m "feat: add PUT /admin/drum-kits/{id} for drum kit edit"
```

---

## Task 5: Drum kit sample audio replacement

**Files:**
- Modify: `app/services/drum_kit_service.py`
- Modify: `app/routers/admin.py`
- Modify: `tests/routers/test_admin_edit.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/routers/test_admin_edit.py`:

```python
# --- Drum kit PATCH /samples/{sample_id} tests ---

@pytest.mark.asyncio
async def test_replace_drum_sample_audio_sets_processing_and_queues_celery(client, db_session):
    user = await _create_user(db_session, role=UserRole.producer)
    kit, sample = await _create_drum_kit(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    mock_task = MagicMock()
    with patch("app.services.drum_kit_service.validate_wav_upload", new=AsyncMock(return_value=b"fakewav")), \
         patch("app.services.drum_kit_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.drum_kit_service.s3_service.delete_object", new=AsyncMock()), \
         patch("app.routers.admin.process_drum_sample_upload", mock_task):
        resp = await client.patch(
            f"/api/v1/admin/drum-kits/{kit.id}/samples/{sample.id}",
            files={"file": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "processing"
    mock_task.delay.assert_called_once_with(str(sample.id))


@pytest.mark.asyncio
async def test_replace_drum_sample_audio_returns_404_if_sample_not_in_kit(client, db_session):
    user = await _create_user(db_session, role=UserRole.producer)
    kit, sample = await _create_drum_kit(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    wrong_kit_id = uuid.uuid4()

    with patch("app.services.drum_kit_service.validate_wav_upload", new=AsyncMock(return_value=b"fakewav")), \
         patch("app.services.drum_kit_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.drum_kit_service.s3_service.delete_object", new=AsyncMock()):
        resp = await client.patch(
            f"/api/v1/admin/drum-kits/{wrong_kit_id}/samples/{sample.id}",
            files={"file": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_edit.py::test_replace_drum_sample_audio_sets_processing_and_queues_celery tests/routers/test_admin_edit.py::test_replace_drum_sample_audio_returns_404_if_sample_not_in_kit -v
```

Expected: `FAILED` (route not found) or `SKIPPED`.

- [ ] **Step 3: Add `replace_sample_audio` to `app/services/drum_kit_service.py`**

Add after `update_drum_kit`. Also ensure `validate_wav_upload` is imported at the top of `drum_kit_service.py` — it already is from `app.utils.audio_validator import validate_wav_upload`.

```python
async def replace_sample_audio(
    db: AsyncSession,
    kit_id: uuid.UUID,
    sample_id: uuid.UUID,
    file: UploadFile,
) -> DrumSample:
    sample = await db.get(DrumSample, sample_id)
    if not sample or sample.drum_kit_id != kit_id:
        raise NotFoundError(f"Sample {sample_id} not found in kit {kit_id}")

    wav_bytes = await validate_wav_upload(file)

    if sample.file_s3_key:
        await s3_service.delete_object(sample.file_s3_key)

    raw_key = s3_service.s3_key_for_raw_drum_sample(str(sample_id))
    await s3_service.upload_bytes(raw_key, wav_bytes, "audio/wav")

    sample.file_s3_key = None
    sample.status = "processing"
    await db.commit()
    await db.refresh(sample)
    return sample
```

- [ ] **Step 4: Add `replace_drum_sample_audio` handler to `app/routers/admin.py`**

Add after the `update_drum_kit` handler:

```python
@router.patch("/drum-kits/{kit_id}/samples/{sample_id}")
async def replace_drum_sample_audio(
    kit_id: uuid.UUID,
    sample_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    sample = await drum_kit_service.replace_sample_audio(db, kit_id, sample_id, file)
    from app.tasks.upload_tasks import process_drum_sample_upload
    process_drum_sample_upload.delay(str(sample_id))
    return success({"sample_id": str(sample.id), "status": sample.status}, "Sample audio replacement queued")
```

- [ ] **Step 5: Run all tests to confirm they pass and there are no regressions**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_edit.py -v
```

Expected: all `PASSED` or `SKIPPED`.

Then run the full suite:

```bash
source .venv/bin/activate && python -m pytest -v
```

Expected: 37+ passed, skipped acceptable. No `FAILED`.

- [ ] **Step 6: Commit**

```bash
git add app/services/drum_kit_service.py app/routers/admin.py tests/routers/test_admin_edit.py
git commit -m "feat: add PATCH /admin/drum-kits/{id}/samples/{sample_id} for sample audio replacement"
```
