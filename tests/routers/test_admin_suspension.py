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
