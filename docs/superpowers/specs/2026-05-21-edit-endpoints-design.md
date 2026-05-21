# Edit Endpoints (Drones, Loops, Drum Kits) — Design Spec

**Date:** 2026-05-21  
**Status:** Approved

## Overview

Add or extend edit endpoints for three entities. All use `multipart/form-data` (consistent with the existing upload pattern). All metadata fields are optional; file fields are optional unless stated otherwise.

---

## Entity 1: Drones

### `PUT /admin/drones/{drone_id}` (extend existing)

Changes from JSON body → `multipart/form-data`. All Form fields are optional:

| Field | Type | Effect |
|---|---|---|
| `title` | `str` | Update `drone.title` |
| `description` | `str` | Update `drone.description` |
| `price` | `Decimal` | Update `drone.price` |
| `is_free` | `bool` | Update `drone.is_free` |
| `category_id` | `UUID` | Update `drone.category_id` (validated against existing categories) |
| `thumbnail` | `UploadFile` | Delete old `drone.thumbnail_s3_key` from S3, upload new file, regenerate `drone.thumbnail_url` via CloudFront |

The `key` Form field (MusicalKey) is removed — per-pad audio replacement uses the sub-resource below.

Service: extend `drone_service.update_drone` to accept an optional thumbnail `UploadFile`. Follows the same thumbnail-upload logic as `drone_service.create_drone` (using `s3_service.s3_key_for_drone_thumbnail`).

Cache invalidation: `delete_pattern("drone:list:*")` after success (already present in handler — no change needed).

---

### `PATCH /admin/drones/{drone_id}/pads/{pad_id}` (new)

Replaces a single pad's audio file.

| Field | Type | Required | Effect |
|---|---|---|---|
| `file` | `UploadFile` (WAV) | Yes | Validate WAV, delete old `pad.raw_s3_key` from S3, upload new WAV, set `pad.status = "processing"`, re-trigger `process_drone_upload.delay(str(pad_id))` |

Auth: `require_producer`.  
Service: new `drone_service.replace_pad_audio(db, pad_id, file)` — validates the pad belongs to `drone_id` before replacing audio; raises `NotFoundError` if not.  
Cache invalidation: `delete_pattern("drone:list:*")` after success.

---

## Entity 2: Loops

### `PUT /admin/loops/{loop_id}` (extend existing)

Changes from JSON body → `multipart/form-data`. All Form fields are optional:

| Field | Type | Effect |
|---|---|---|
| `title` | `str` | Update `loop.title` |
| `description` | `str` | Update `loop.description` |
| `genre` | `Genre` | Update `loop.genre` |
| `bpm` | `int` | Update `loop.bpm` (validated 60–140) |
| `tempo_feel` | `TempoFeel` | Update `loop.tempo_feel` |
| `tags` | `str` (comma-separated, e.g. `"chill,dark,piano"`) | Split on comma, strip whitespace, update `loop.tags` |
| `price` | `Decimal` | Update `loop.price` |
| `is_free` | `bool` | Update `loop.is_free` |
| `thumbnail` | `UploadFile` | Delete old `loop.thumbnail_s3_key` from S3, upload new, update `loop.thumbnail_s3_key` |
| `file` | `UploadFile` (WAV) | Validate WAV, delete old `loop.file_s3_key` from S3, upload new raw WAV via `s3_key_for_raw_loop`, set `loop.status = "processing"`, re-trigger `process_loop_upload.delay(str(loop_id))` |

Since a loop has exactly one audio file, both thumbnail and audio replacement live in the same endpoint.

Service: extend `loop_service.update_loop` to accept optional `thumbnail` and `file` UploadFiles.  
Auth: `require_admin`.

---

## Entity 3: Drum Kits

### `PUT /admin/drum-kits/{kit_id}` (new)

New endpoint. All Form fields are optional:

| Field | Type | Effect |
|---|---|---|
| `title` | `str` | Update `kit.title` |
| `description` | `str` | Update `kit.description` |
| `price` | `Decimal` | Update `kit.price` |
| `is_free` | `bool` | Update `kit.is_free` |
| `thumbnail` | `UploadFile` | Delete old `kit.thumbnail_s3_key` from S3, upload new, update `kit.thumbnail_s3_key` |

Auth: `require_admin`.  
New schema: `DrumKitUpdate` — all fields optional, same validator as `DrumKitCreate` (price required for paid kits).  
New service: `drum_kit_service.update_drum_kit(db, kit_id, data, thumbnail)`.

---

### `PATCH /admin/drum-kits/{kit_id}/samples/{sample_id}` (new)

Replaces a single sample's audio file.

| Field | Type | Required | Effect |
|---|---|---|---|
| `file` | `UploadFile` (WAV) | Yes | Validate WAV, delete old `sample.file_s3_key` from S3, upload new WAV via `s3_key_for_raw_drum_sample`, set `sample.status = "processing"`, re-trigger `process_drum_kit_upload.delay(str(sample_id))` |

Auth: `require_producer`.  
New service: `drum_kit_service.replace_sample_audio(db, kit_id, sample_id, file)` — validates `sample_id` belongs to `kit_id` before replacing audio; raises `NotFoundError` if not.

---

## Cross-Cutting Concerns

### WAV Validation

All audio file replacements call `validate_wav_upload(file)` before touching S3 — same as upload paths.

### S3 Deletion

Before uploading a replacement file, delete the old S3 object if the key exists:
```python
if old_key:
    await s3_service.delete_object(old_key)
```

### Error Handling

- Entity not found → `NotFoundError` (404)
- Pad/sample not found or not belonging to parent → `NotFoundError` (404)
- Paid entity with no price → `AppError` (422)
- Invalid WAV → raised by `validate_wav_upload`

### Auth

| Endpoint | Dependency |
|---|---|
| `PUT /admin/drones/{id}` | `require_admin` (existing) |
| `PATCH /admin/drones/{id}/pads/{pad_id}` | `require_producer` |
| `PUT /admin/loops/{id}` | `require_admin` (existing) |
| `PUT /admin/drum-kits/{id}` | `require_admin` |
| `PATCH /admin/drum-kits/{id}/samples/{sample_id}` | `require_producer` |

### Response Envelope

All endpoints return the standard `{"status": "success", "data": {...}, "message": "..."}` envelope.

---

## Files Changed

| File | Change |
|---|---|
| `app/schemas/drone_pad.py` | Remove `thumbnail_url` from `DronePadUpdate` (replaced by file upload) |
| `app/schemas/drum_kit.py` | Add `DrumKitUpdate` schema |
| `app/services/drone_service.py` | Extend `update_drone` + add `replace_pad_audio` |
| `app/services/loop_service.py` | Extend `update_loop` |
| `app/services/drum_kit_service.py` | Add `update_drum_kit` + `replace_sample_audio` |
| `app/routers/admin.py` | Extend `update_loop`, `update_drone`; add `update_drum_kit`, `replace_drum_sample`, `replace_drone_pad` |
| `tests/routers/test_admin_edit.py` | New test file covering all 5 endpoints |
