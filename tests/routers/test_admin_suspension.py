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
    from unittest.mock import AsyncMock, patch
    user = await _make_user(db_session)
    # DB flag is False — not suspended in DB
    assert user.is_suspended is False
    token = create_access_token(str(user.id), user.role.value)
    # Patch Redis to simulate a suspended key being present
    with patch("app.middleware.auth_middleware.get_redis") as mock_get_redis:
        mock_redis = AsyncMock()
        mock_redis.exists = AsyncMock(return_value=1)
        mock_redis.aclose = AsyncMock()
        mock_get_redis.return_value = mock_redis
        # This test verifies the logic, not the full HTTP stack with fixture override
        from app.middleware.auth_middleware import get_current_user
        from fastapi.security import HTTPAuthorizationCredentials
        from app.exceptions import UnauthorizedError
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        try:
            await get_current_user(credentials=creds, db=db_session, redis=mock_redis)
            assert False, "Should have raised UnauthorizedError"
        except UnauthorizedError as e:
            assert "suspended" in str(e).lower()
