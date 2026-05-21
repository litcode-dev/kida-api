# Drone List Caching — Design Spec

**Date:** 2026-05-21  
**Status:** Approved

## Overview

Cache the `GET /api/v1/drones` response in Redis. Invalidate the cache whenever an admin or producer creates, updates, or deletes a drone. All other drone read endpoints (`/drones/{id}`, `/drones/{id}/preview`, `/drones/{id}/download`) are out of scope.

## Architecture

Uses the existing `cache_service` (Redis-backed, `litmusic:cache:` namespace). No new dependencies or infrastructure.

## Cache Key

Filter params are encoded in a fixed, stable order:

```
drone:list:{key}:{is_free}:{category_id}:{page}:{page_size}
```

- Missing/None params are represented as the string `"none"`.
- Example: `drone:list:Am:none:none:1:50`
- Example (unfiltered default): `drone:list:none:none:none:1:50`

## TTL

`TTL_DRONE_LIST = 300` (5 minutes), added to `cache_service.py`. Acts as a safety net; explicit invalidation is the primary mechanism.

## Read Path (`app/routers/drones.py`)

`list_drones` handler:

1. Build cache key from `(key, is_free, category_id, page, page_size)`.
2. Call `cache_service.get(cache_key)`.
3. On hit: return `success(cached)` immediately.
4. On miss: run DB query via `drone_service.list_drones`, serialize with `DroneResponse`, write to Redis with `TTL_DRONE_LIST`, then return.

## Invalidation Path (`app/routers/admin.py`)

Four write operations each call `cache_service.delete_pattern("drone:list:*")` after the DB operation succeeds, inside a `try/except` that logs a warning on failure (same guard used by category invalidation):

| Endpoint | Trigger |
|---|---|
| `POST /admin/drones` | After `drone_service.create_drone` |
| `POST /admin/drones/bulk` | After `drone_service.bulk_create_drones` |
| `PUT /admin/drones/{drone_id}` | After `drone_service.update_drone` |
| `DELETE /admin/drones/{drone_id}` | After `drone_service.delete_drone` |

## Files Changed

| File | Change |
|---|---|
| `app/services/cache_service.py` | Add `TTL_DRONE_LIST = 300` |
| `app/routers/drones.py` | Add cache read/write in `list_drones` |
| `app/routers/admin.py` | Add `delete_pattern` calls in 4 write handlers |

## What Is Not Changing

- `GET /api/v1/drones/{drone_id}` — no caching (cheap single-row lookup).
- `cache_service` internals — no structural changes.
- Auth, rate limiting, response envelope — unchanged.
