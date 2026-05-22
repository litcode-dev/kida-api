# User Suspension — Design Spec

**Date:** 2026-05-22  
**Status:** Approved

---

## Overview

Add the ability for admins to suspend and unsuspend users. Suspended users are immediately locked out — existing JWTs are invalidated instantly via a Redis blocklist — and cannot log back in until unsuspended. Admins cannot suspend other admins.

---

## Endpoints

Both endpoints are in `app/routers/admin.py`, protected by `require_admin` and rate-limited at `10/minute`.

### PUT /admin/users/{user_id}/suspend

Suspends a user. Sets `is_suspended=True` in the database and writes a Redis key to invalidate any active tokens immediately.

**Path param:** `user_id` (UUID)

**Guards:**
- Target user must exist → 404 if not
- Target user must not be an admin → 403 if admin

**Response (200):**
```json
{ "status": "success", "data": { <UserResponse> }, "message": "User suspended" }
```

### PUT /admin/users/{user_id}/unsuspend

Unsuspends a user. Sets `is_suspended=False` in the database and deletes the Redis blocklist key, restoring login and API access.

**Path param:** `user_id` (UUID)

**Guards:**
- Target user must exist → 404 if not

**Response (200):**
```json
{ "status": "success", "data": { <UserResponse> }, "message": "User unsuspended" }
```

---

## Data Model

### `app/models/user.py`

Add one column to the `User` model:

```python
is_suspended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

### Alembic migration

New migration file adds `is_suspended BOOLEAN NOT NULL DEFAULT FALSE` to the `users` table.

---

## Enforcement

### `app/middleware/auth_middleware.py` — `get_current_user`

After loading the user from DB, check the Redis blocklist:

```python
redis_key = f"suspended:{user.id}"
if await redis.exists(redis_key) or user.is_suspended:
    raise UnauthorizedError("Account suspended")
```

`get_current_user` already has a `db` dependency. A `redis: Redis = Depends(get_redis)` dependency is added alongside it. `get_redis` is already defined in this file.

### `app/services/auth_service.py` — `authenticate_user`

After verifying credentials, check `is_suspended` before issuing a token:

```python
if user.is_suspended:
    raise UnauthorizedError("Account suspended")
```

---

## Redis Keys

| Operation | Redis action |
|-----------|-------------|
| Suspend | `await redis.set(f"suspended:{user_id}", "1")` — no expiry |
| Unsuspend | `await redis.delete(f"suspended:{user_id}")` |

No expiry is set — the key persists until explicitly deleted on unsuspend. The DB `is_suspended` field is the source of truth; Redis is the fast-path check that avoids a DB hit on every request.

---

## `app/schemas/user.py`

`UserResponse` must expose `is_suspended` so the endpoint response reflects the new state. Add the field if not already present.

---

## Files Changed

| Action | File |
|--------|------|
| Modify | `app/models/user.py` — add `is_suspended` column |
| Create | `alembic/versions/<hash>_add_is_suspended_to_users.py` |
| Modify | `app/middleware/auth_middleware.py` — blocklist check in `get_current_user` |
| Modify | `app/services/auth_service.py` — suspension check in `authenticate_user` |
| Modify | `app/routers/admin.py` — add 2 endpoints |
| Modify | `app/schemas/user.py` — add `is_suspended` to `UserResponse` |

---

## Out of Scope

- Suspension reason / audit log
- Notifying the user by email on suspension
- Time-limited suspensions (auto-unsuspend after N days)
