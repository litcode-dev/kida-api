# Producer/Admin Router Split — Design Spec

**Date:** 2026-05-22  
**Status:** Approved

---

## Overview

`app/routers/admin.py` currently contains a mix of producer-owned content endpoints (upload, update, status) and true admin-only operations (moderation, user management, analytics). All 18 producer-owned endpoints are moved to `app/routers/producer.py` under the `/producer` prefix. `admin.py` is left with only the 12 strictly admin-only endpoints. Four PUT endpoints that were incorrectly guarded with `require_admin` are corrected to `require_producer` during the move.

---

## URL Changes (Breaking)

Old URL (removed) → New URL (in `producer.py`)

| Old | New |
|-----|-----|
| POST /admin/loops | POST /producer/loops |
| GET /admin/loops/{id}/status | GET /producer/loops/{id}/status |
| PUT /admin/loops/{id} | PUT /producer/loops/{id} |
| POST /admin/stem-packs | POST /producer/stem-packs |
| POST /admin/stem-packs/{id}/stems | POST /producer/stem-packs/{id}/stems |
| PUT /admin/stem-packs/{id} | PUT /producer/stem-packs/{id} |
| POST /admin/drones/categories | POST /producer/drones/categories |
| GET /admin/drones/categories | GET /producer/drones/categories |
| POST /admin/drones | POST /producer/drones |
| POST /admin/drones/bulk | POST /producer/drones/bulk |
| GET /admin/drones/bulk/status | GET /producer/drones/bulk/status |
| GET /admin/drones/{id}/status | GET /producer/drones/{id}/status |
| PUT /admin/drones/{id} | PUT /producer/drones/{id} |
| PATCH /admin/drones/{id}/pads/{pad_id} | PATCH /producer/drones/{id}/pads/{pad_id} |
| POST /admin/drum-kits | POST /producer/drum-kits |
| GET /admin/drum-kits | GET /producer/drum-kits |
| PUT /admin/drum-kits/{id} | PUT /producer/drum-kits/{id} |
| PATCH /admin/drum-kits/{id}/samples/{sample_id} | PATCH /producer/drum-kits/{id}/samples/{sample_id} |

---

## Permission Corrections

These 4 endpoints were `require_admin` in `admin.py` — they must become `require_producer` in `producer.py` (producers own their own content updates):

- PUT /producer/loops/{id}
- PUT /producer/stem-packs/{id}
- PUT /producer/drones/{id}
- PUT /producer/drum-kits/{id}

All other moved endpoints already used `require_producer` and need no permission change.

---

## Admin-Only Endpoints (Remain in `admin.py`)

These 12 endpoints stay at `/admin/*` and keep `require_admin`:

| Method | Path |
|--------|------|
| POST | /admin/email/test |
| DELETE | /admin/loops/{id} |
| DELETE | /admin/stem-packs/{id} |
| GET | /admin/users |
| PUT | /admin/users/{id}/role |
| PUT | /admin/users/{id}/ai-enabled |
| GET | /admin/ai/generations |
| DELETE | /admin/drones/categories/{id} |
| DELETE | /admin/drones/{id} |
| DELETE | /admin/drum-kits/{id} |
| GET | /admin/newsletter/subscribers |
| GET | /admin/analytics |

---

## File Changes

### `app/routers/producer.py`

**Modified.** 18 endpoint functions appended (moved verbatim from `admin.py`). Additional imports added to match the moved functions:

New imports needed (beyond what already exists):
- `from fastapi import UploadFile, File, Form` (add to existing fastapi import)
- `from decimal import Decimal`
- `from sqlalchemy import select, func`
- `from app.services import loop_service, stem_pack_service, drone_service, drum_kit_service, cache_service`
- `from app.schemas.loop import LoopCreate, LoopUpdate, LoopResponse`
- `from app.schemas.stem_pack import StemPackCreate, StemCreate, StemPackResponse, StemResponse`
- `from app.schemas.drone_pad import DronePadCategoryCreate, DronePadCategoryResponse, DronePadCreate, DronePadUpdate, DroneResponse`
- `from app.schemas.drum_kit import DrumKitCreate, DrumKitResponse, DrumKitUpdate`
- `from app.models.loop import Genre, TempoFeel`
- `from app.models.drone_pad import MusicalKey`
- `from app.models.user import User`
- `from app.exceptions import NotFoundError`
- `import uuid`
- `from app.tasks.upload_tasks import process_drone_upload, process_loop_upload, process_drum_sample_upload`
- `from app.tasks.notification_tasks import send_new_content_emails`

### `app/routers/admin.py`

**Modified.** 18 endpoint functions and their associated `# --- section ---` comments removed. Imports that are no longer needed after the removal are also cleaned up.

### `app/main.py`

**No changes.** `producer.py` is already registered.

---

## What Does NOT Change

- All service calls are identical (no changes to `loop_service`, `drone_service`, etc.)
- All schemas are identical
- All task calls are identical
- The `require_producer` dependency already allows both `producer` and `admin` roles, so admins retain full access to the producer endpoints
- No database migrations needed

---

## Out of Scope

- Rate limiting or per-producer ownership checks (e.g. ensuring a producer can only update their own content) — this would require a separate feature
- Any other routers (`loops.py`, `drones.py`, etc.) — only `admin.py` and `producer.py` are touched
