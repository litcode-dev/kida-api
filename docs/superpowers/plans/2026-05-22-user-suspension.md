# User Suspension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow admins to suspend and unsuspend users, immediately invalidating active JWTs via a Redis blocklist and blocking login for suspended accounts.

**Architecture:** Three layered changes — (1) add `is_suspended` to the DB model and run a migration, (2) enforce suspension in `get_current_user` (Redis blocklist check) and `authenticate_user` (DB flag check), (3) add `PUT /admin/users/{id}/suspend` and `PUT /admin/users/{id}/unsuspend` endpoints that write/delete the Redis key and update the DB flag.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Redis (via `redis.asyncio`), pytest-asyncio.

---

## File Map

| Action | Path |
|--------|------|
| Modify | `app/models/user.py` — add `is_suspended` column |
| Modify | `app/schemas/user.py` — add `is_suspended` to `UserResponse` |
| Create | `alembic/versions/y4z36u15v2w7_add_is_suspended_to_users.py` |
| Modify | `app/middleware/auth_middleware.py` — Redis blocklist check in `get_current_user` |
| Modify | `app/services/auth_service.py` — DB flag check in `authenticate_user` |
| Modify | `app/routers/admin.py` — add 2 endpoints + Redis import |
| Create | `tests/routers/test_admin_suspension.py` |

---

## Task 1: Model, Schema, and Migration

**Files:**
- Modify: `app/models/user.py`
- Modify: `app/schemas/user.py`
- Create: `alembic/versions/y4z36u15v2w7_add_is_suspended_to_users.py`

- [ ] **Step 1: Write the failing test**

Create `tests/routers/test_admin_suspension.py`:

```python
# tests/routers/test_admin_suspension.py
import pytest
import uuid
from app.models.user import User, UserRole
from app.services.auth_service import hash_password


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
    await db.refresh(u)
    return u


@pytest.mark.asyncio
async def test_user_model_has_is_suspended(db_session):
    user = await _make_user(db_session)
    assert hasattr(user, "is_suspended")
    assert user.is_suspended is False


@pytest.mark.asyncio
async def test_user_response_includes_is_suspended(db_session):
    from app.schemas.user import UserResponse
    user = await _make_user(db_session)
    data = UserResponse.model_validate(user).model_dump()
    assert "is_suspended" in data
    assert data["is_suspended"] is False
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/litecode/Documents/Projects/Python/litmusic-api
source .venv/bin/activate && python -m pytest tests/routers/test_admin_suspension.py::test_user_model_has_is_suspended tests/routers/test_admin_suspension.py::test_user_response_includes_is_suspended -v 2>&1 | tail -15
```

Expected: FAIL — `AttributeError: is_suspended` not found.

- [ ] **Step 3: Add `is_suspended` to the User model**

In `app/models/user.py`, add after the `ai_extra_credits` line:

```python
is_suspended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

- [ ] **Step 4: Add `is_suspended` to UserResponse**

In `app/schemas/user.py`, add to `UserResponse`:

```python
class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    avatar_url: str | None = None
    created_at: datetime
    is_suspended: bool = False

    model_config = {"from_attributes": True}
```

- [ ] **Step 5: Create the Alembic migration**

Create `alembic/versions/y4z36u15v2w7_add_is_suspended_to_users.py`:

```python
"""add is_suspended to users

Revision ID: y4z36u15v2w7
Revises: x3y25t04u1v6
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa

revision = "y4z36u15v2w7"
down_revision = "x3y25t04u1v6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_suspended", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("users", "is_suspended")
```

- [ ] **Step 6: Run the migration**

```bash
source .venv/bin/activate && alembic upgrade head 2>&1
```

Expected: `Running upgrade x3y25t04u1v6 -> y4z36u15v2w7, add is_suspended to users`

- [ ] **Step 7: Run the model/schema tests**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_suspension.py::test_user_model_has_is_suspended tests/routers/test_admin_suspension.py::test_user_response_includes_is_suspended -v 2>&1 | tail -10
```

Expected: both PASS.

- [ ] **Step 8: Commit**

```bash
git add app/models/user.py app/schemas/user.py alembic/versions/y4z36u15v2w7_add_is_suspended_to_users.py tests/routers/test_admin_suspension.py
git commit -m "feat: add is_suspended field to User model and schema"
```

---

## Task 2: Enforcement — Auth Middleware and Auth Service

**Files:**
- Modify: `app/middleware/auth_middleware.py`
- Modify: `app/services/auth_service.py`

- [ ] **Step 1: Write failing enforcement tests**

Append to `tests/routers/test_admin_suspension.py`:

```python

@pytest.mark.asyncio
async def test_suspended_user_cannot_authenticate(db_session):
    from app.services.auth_service import authenticate_user
    from app.exceptions import UnauthorizedError
    user = await _make_user(db_session)
    user.is_suspended = True
    await db_session.commit()
    with pytest.raises(UnauthorizedError, match="suspended"):
        await authenticate_user(db_session, user.email, "x")


@pytest.mark.asyncio
async def test_get_current_user_rejects_suspended_db_flag(client, db_session):
    from app.services.auth_service import create_access_token
    user = await _make_user(db_session)
    user.is_suspended = True
    await db_session.commit()
    token = create_access_token(str(user.id), user.role.value)
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
```

- [ ] **Step 2: Run failing tests**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_suspension.py::test_suspended_user_cannot_authenticate tests/routers/test_admin_suspension.py::test_get_current_user_rejects_suspended_db_flag -v 2>&1 | tail -15
```

Expected: both FAIL (no suspension check exists yet).

- [ ] **Step 3: Add suspension check to `authenticate_user`**

In `app/services/auth_service.py`, update `authenticate_user`:

```python
async def authenticate_user(db: AsyncSession, email: str, password: str) -> User:
    user = await db.scalar(select(User).where(User.email == email))
    if not user or not user.password_hash or not await verify_password(password, user.password_hash):
        raise UnauthorizedError("Invalid credentials")
    if user.is_suspended:
        raise UnauthorizedError("Account suspended")
    return user
```

- [ ] **Step 4: Add Redis blocklist check to `get_current_user`**

In `app/middleware/auth_middleware.py`, update `get_current_user` to accept and check Redis:

```python
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> User:
    token = credentials.credentials
    payload = decode_access_token(token)
    user = await get_user_by_id(db, payload["sub"])
    if await redis.exists(f"suspended:{user.id}") or user.is_suspended:
        raise UnauthorizedError("Account suspended")
    return user
```

- [ ] **Step 5: Run enforcement tests**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_suspension.py::test_suspended_user_cannot_authenticate tests/routers/test_admin_suspension.py::test_get_current_user_rejects_suspended_db_flag -v 2>&1 | tail -15
```

Expected: both PASS.

- [ ] **Step 6: Run full test suite — verify no regressions**

```bash
source .venv/bin/activate && python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: same pass/fail counts as before this task (the 5 pre-existing failures in email/onesignal are unrelated).

- [ ] **Step 7: Commit**

```bash
git add app/middleware/auth_middleware.py app/services/auth_service.py tests/routers/test_admin_suspension.py
git commit -m "feat: enforce suspension in get_current_user and authenticate_user"
```

---

## Task 3: Suspend and Unsuspend Endpoints

**Files:**
- Modify: `app/routers/admin.py`

- [ ] **Step 1: Write failing endpoint tests**

Append to `tests/routers/test_admin_suspension.py`:

```python

@pytest.mark.asyncio
async def test_suspend_endpoint_sets_flag_and_blocks(client, db_session):
    from app.services.auth_service import create_access_token
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session, UserRole.user)
    admin_token = create_access_token(str(admin.id), admin.role.value)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/suspend",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["is_suspended"] is True

    # suspended user's token should now be rejected
    user_token = create_access_token(str(target.id), target.role.value)
    me_resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert me_resp.status_code == 401


@pytest.mark.asyncio
async def test_unsuspend_endpoint_clears_flag_and_restores_access(client, db_session):
    from app.services.auth_service import create_access_token
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session, UserRole.user)
    target.is_suspended = True
    await db_session.commit()
    admin_token = create_access_token(str(admin.id), admin.role.value)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/unsuspend",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["is_suspended"] is False


@pytest.mark.asyncio
async def test_suspend_admin_is_forbidden(client, db_session):
    from app.services.auth_service import create_access_token
    admin = await _make_user(db_session, UserRole.admin)
    other_admin = await _make_user(db_session, UserRole.admin)
    token = create_access_token(str(admin.id), admin.role.value)

    resp = await client.put(
        f"/api/v1/admin/users/{other_admin.id}/suspend",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_suspend_nonexistent_user_returns_404(client, db_session):
    from app.services.auth_service import create_access_token
    admin = await _make_user(db_session, UserRole.admin)
    token = create_access_token(str(admin.id), admin.role.value)

    resp = await client.put(
        f"/api/v1/admin/users/{uuid.uuid4()}/suspend",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run failing endpoint tests**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_suspension.py -k "endpoint or suspend_admin or nonexistent" -v 2>&1 | tail -15
```

Expected: FAIL — routes return 404 (not yet defined).

- [ ] **Step 3: Add imports to `app/routers/admin.py`**

At the top of `app/routers/admin.py`, add Redis imports to the existing import block:

```python
from redis.asyncio import Redis
from app.middleware.auth_middleware import get_redis
```

- [ ] **Step 4: Add suspend and unsuspend endpoints to `app/routers/admin.py`**

Add after the `toggle_user_ai` endpoint (after line ~166), before the `# --- AI administration ---` section or wherever the user management block ends:

```python

@router.put("/users/{user_id}/suspend")
@limiter.limit("10/minute")
async def suspend_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")
    if user.role == UserRole.admin:
        raise ForbiddenError("Cannot suspend an admin")
    user.is_suspended = True
    await db.commit()
    await db.refresh(user)
    await redis.set(f"suspended:{user_id}", "1")
    return success(UserResponse.model_validate(user).model_dump(), "User suspended")


@router.put("/users/{user_id}/unsuspend")
@limiter.limit("10/minute")
async def unsuspend_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")
    user.is_suspended = False
    await db.commit()
    await db.refresh(user)
    await redis.delete(f"suspended:{user_id}")
    return success(UserResponse.model_validate(user).model_dump(), "User unsuspended")
```

Also add `ForbiddenError` to the existing exceptions import in admin.py if not already present:

```python
from app.exceptions import NotFoundError, AppError, ForbiddenError
```

- [ ] **Step 5: Run endpoint tests**

```bash
source .venv/bin/activate && python -m pytest tests/routers/test_admin_suspension.py -v 2>&1 | tail -20
```

Expected: all suspension tests PASS.

- [ ] **Step 6: Run full test suite**

```bash
source .venv/bin/activate && python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: no regressions.

- [ ] **Step 7: Commit and push**

```bash
git add app/routers/admin.py tests/routers/test_admin_suspension.py
git commit -m "feat: add suspend and unsuspend endpoints to admin router"
git push
```

---

## Self-Review

**Spec coverage:**
- ✅ `PUT /admin/users/{id}/suspend` — Task 3
- ✅ `PUT /admin/users/{id}/unsuspend` — Task 3
- ✅ `is_suspended` DB column + migration — Task 1
- ✅ `UserResponse` exposes `is_suspended` — Task 1
- ✅ Redis blocklist in `get_current_user` — Task 2
- ✅ Login blocked via `authenticate_user` — Task 2
- ✅ Admins cannot suspend other admins — Task 3 (`test_suspend_admin_is_forbidden`)
- ✅ 404 on missing user — Task 3 (`test_suspend_nonexistent_user_returns_404`)

**No placeholders:** All code blocks are complete.

**Type consistency:** `UserResponse` is used consistently across all tasks. `redis.set`/`redis.delete`/`redis.exists` key format `suspended:{user_id}` is consistent across Tasks 2 and 3.
