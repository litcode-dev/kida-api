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


@pytest.mark.asyncio
async def test_get_current_user_rejects_suspended_redis_key(client, db_session):
    """Suspension via Redis key works even when DB flag is False."""
    from app.services.auth_service import create_access_token
    from unittest.mock import AsyncMock
    user = await _make_user(db_session)
    assert user.is_suspended is False
    token = create_access_token(str(user.id), user.role.value)
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value="Violating community guidelines")
    mock_redis.aclose = AsyncMock()
    from app.middleware.auth_middleware import get_current_user
    from fastapi.security import HTTPAuthorizationCredentials
    from app.exceptions import UnauthorizedError
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    try:
        await get_current_user(credentials=creds, db=db_session, redis=mock_redis)
        assert False, "Should have raised UnauthorizedError"
    except UnauthorizedError as e:
        assert "suspended" in str(e).lower()


@pytest.mark.asyncio
async def test_suspend_endpoint_sets_flag_and_returns_suspended_true(client, db_session):
    from app.services.auth_service import create_access_token
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session, UserRole.user)
    token = create_access_token(str(admin.id), admin.role.value)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/suspend",
        json={"reason": "Violating community guidelines"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["is_suspended"] is True
    assert data["suspension_reason"] == "Violating community guidelines"


@pytest.mark.asyncio
async def test_unsuspend_endpoint_clears_flag(client, db_session):
    from app.services.auth_service import create_access_token
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session, UserRole.user)
    target.is_suspended = True
    await db_session.commit()
    token = create_access_token(str(admin.id), admin.role.value)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/unsuspend",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["is_suspended"] is False


@pytest.mark.asyncio
async def test_unsuspend_clears_reason(client, db_session):
    from app.services.auth_service import create_access_token
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session, UserRole.user)
    target.is_suspended = True
    target.suspension_reason = "Spam"
    await db_session.commit()
    token = create_access_token(str(admin.id), admin.role.value)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/unsuspend",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["is_suspended"] is False
    assert data["suspension_reason"] is None


@pytest.mark.asyncio
async def test_suspend_admin_returns_403(client, db_session):
    from app.services.auth_service import create_access_token
    admin = await _make_user(db_session, UserRole.admin)
    other_admin = await _make_user(db_session, UserRole.admin)
    token = create_access_token(str(admin.id), admin.role.value)

    resp = await client.put(
        f"/api/v1/admin/users/{other_admin.id}/suspend",
        json={"reason": "Test"},
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
        json={"reason": "Test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
