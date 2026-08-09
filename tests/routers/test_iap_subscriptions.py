import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.iap_subscription import IapSubscription, IapSubscriptionStatus
from app.models.user import User, UserRole
from app.services.auth_service import hash_password, create_access_token


async def _create_user(db):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"), full_name="Test",
        role=UserRole.user,
    )
    db.add(user)
    await db.commit()
    return user


def _auth_headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _add_sub(db, user_id, status, expires_at):
    sub = IapSubscription(
        user_id=user_id, platform="android", product_id="kida.premium.monthly",
        store_transaction_id=str(uuid.uuid4()), status=status, expires_at=expires_at,
    )
    db.add(sub)
    await db.commit()
    return sub


@pytest.mark.asyncio
async def test_me_requires_auth(client):
    resp = await client.get("/api/v1/subscriptions/me")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_me_none(client, db_session):
    user = await _create_user(db_session)
    resp = await client.get("/api/v1/subscriptions/me", headers=_auth_headers(user))
    assert resp.status_code == 200
    assert resp.json() == {"active": False, "expires_at": None, "product_id": None}


@pytest.mark.asyncio
async def test_me_active(client, db_session):
    user = await _create_user(db_session)
    exp = datetime.now(timezone.utc) + timedelta(days=15)
    await _add_sub(db_session, user.id, IapSubscriptionStatus.active, exp)
    resp = await client.get("/api/v1/subscriptions/me", headers=_auth_headers(user))
    body = resp.json()
    assert resp.status_code == 200
    assert body["active"] is True
    assert body["product_id"] == "kida.premium.monthly"
    assert body["expires_at"] is not None


@pytest.mark.asyncio
async def test_me_expired(client, db_session):
    user = await _create_user(db_session)
    exp = datetime.now(timezone.utc) - timedelta(days=1)
    await _add_sub(db_session, user.id, IapSubscriptionStatus.active, exp)
    resp = await client.get("/api/v1/subscriptions/me", headers=_auth_headers(user))
    body = resp.json()
    assert resp.status_code == 200
    assert body["active"] is False


@pytest.mark.asyncio
async def test_verify_android_creates_entitlement(client, db_session):
    user = await _create_user(db_session)

    resp = await client.post(
        "/api/v1/subscriptions/verify",
        json={"product_id": "kida.premium.monthly", "platform": "android", "receipt": "{...}"},
        headers=_auth_headers(user),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["product_id"] == "kida.premium.monthly"


@pytest.mark.asyncio
async def test_verify_rejects_bad_platform(client, db_session):
    user = await _create_user(db_session)
    resp = await client.post(
        "/api/v1/subscriptions/verify",
        json={"product_id": "kida.premium.monthly", "platform": "windows", "receipt": "x"},
        headers=_auth_headers(user),
    )
    assert resp.status_code == 422
