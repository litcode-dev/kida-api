"""Every endpoint that hands out tokens also names the user, so the app can
call OneSignal.login with the same id the API addresses pushes to."""
import uuid

import pytest

from app.models.user import User, UserRole
from app.services.auth_service import hash_password

EMAIL = "tokens@test.com"
PASSWORD = "Password123!"


async def _make_user(db):
    user = User(
        id=uuid.uuid4(),
        email=EMAIL,
        password_hash=await hash_password(PASSWORD),
        full_name="Token Holder",
        role=UserRole.user,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    return user


async def _login(client):
    resp = await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


@pytest.mark.asyncio
async def test_login_returns_the_user_id(client, db_session):
    user = await _make_user(db_session)

    data = await _login(client)

    assert data["user_id"] == str(user.id)
    assert data["full_name"] == "Token Holder"
    assert data["role"] == "user"


@pytest.mark.asyncio
async def test_refresh_issues_new_tokens_with_the_user_id(client, db_session):
    user = await _make_user(db_session)
    login = await _login(client)

    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["user_id"] == str(user.id)
    assert data["full_name"] == "Token Holder"
    assert data["refresh_token"] != login["refresh_token"]

    # The old refresh token is spent.
    again = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert again.status_code == 401
