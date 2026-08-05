import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.iap_subscription import (
    IapSubscription,
    IapSubscriptionSource,
    IapSubscriptionStatus,
)
from app.models.user import User, UserRole
from app.services.auth_service import hash_password, create_access_token
from app.services.revenuecat_service import RevenueCatClient, RevenueCatEntitlement


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


def _future_ms(days=30):
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp() * 1000)


async def _get_sub(db, user_id):
    return await db.scalar(
        select(IapSubscription).where(IapSubscription.user_id == user_id)
    )


# --- webhook -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_rejects_bad_auth(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    resp = await client.post(
        "/api/v1/subscriptions/webhook/revenuecat",
        headers={"Authorization": "wrong"},
        json={"event": {"type": "INITIAL_PURCHASE"}},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_initial_purchase_creates_entitlement(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "premium")
    user = await _create_user(db_session)

    resp = await client.post(
        "/api/v1/subscriptions/webhook/revenuecat",
        headers={"Authorization": "hook-secret"},
        json={"event": {
            "type": "INITIAL_PURCHASE",
            "app_user_id": str(user.id),
            "product_id": "kida.premium.yearly",
            "entitlement_ids": ["premium"],
            "expiration_at_ms": _future_ms(365),
            "store": "APP_STORE",
            "original_transaction_id": "orig-1",
        }},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is True

    sub = await _get_sub(db_session, user.id)
    assert sub is not None
    assert sub.status == IapSubscriptionStatus.active
    assert sub.source == IapSubscriptionSource.revenuecat
    assert sub.product_id == "kida.premium.yearly"
    assert sub.app_user_id == str(user.id)
    assert sub.store_transaction_id == "orig-1"


@pytest.mark.asyncio
async def test_webhook_expiration_marks_expired(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    user = await _create_user(db_session)

    resp = await client.post(
        "/api/v1/subscriptions/webhook/revenuecat",
        headers={"Authorization": "hook-secret"},
        json={"event": {
            "type": "EXPIRATION",
            "app_user_id": str(user.id),
            "product_id": "kida.premium.monthly",
            "expiration_at_ms": int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000),
            "store": "PLAY_STORE",
        }},
    )
    assert resp.status_code == 200
    sub = await _get_sub(db_session, user.id)
    assert sub.status == IapSubscriptionStatus.expired


@pytest.mark.asyncio
async def test_webhook_respects_admin_lock(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    user = await _create_user(db_session)
    # Admin has revoked & locked the entitlement.
    locked = IapSubscription(
        user_id=user.id, product_id="kida.premium.monthly",
        store_transaction_id=str(uuid.uuid4()), status=IapSubscriptionStatus.revoked,
        admin_locked=True,
    )
    db_session.add(locked)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/subscriptions/webhook/revenuecat",
        headers={"Authorization": "hook-secret"},
        json={"event": {
            "type": "RENEWAL",
            "app_user_id": str(user.id),
            "product_id": "kida.premium.monthly",
            "expiration_at_ms": _future_ms(30),
            "store": "APP_STORE",
        }},
    )
    assert resp.status_code == 200
    sub = await _get_sub(db_session, user.id)
    # Lock held — RevenueCat did not restore the entitlement.
    assert sub.status == IapSubscriptionStatus.revoked
    assert sub.admin_locked is True


@pytest.mark.asyncio
async def test_webhook_test_event_no_op(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    resp = await client.post(
        "/api/v1/subscriptions/webhook/revenuecat",
        headers={"Authorization": "hook-secret"},
        json={"event": {"type": "TEST"}},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is False


@pytest.mark.asyncio
async def test_webhook_anonymous_user_no_op(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    resp = await client.post(
        "/api/v1/subscriptions/webhook/revenuecat",
        headers={"Authorization": "hook-secret"},
        json={"event": {
            "type": "INITIAL_PURCHASE",
            "app_user_id": "$RCAnonymousID:abc123",
            "product_id": "kida.premium.monthly",
            "expiration_at_ms": _future_ms(30),
        }},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is False


# --- REST reconcile ----------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_revenuecat_pulls_entitlement(client, db_session, monkeypatch):
    user = await _create_user(db_session)
    exp = datetime.now(timezone.utc) + timedelta(days=20)
    monkeypatch.setattr(
        RevenueCatClient, "fetch_entitlement",
        AsyncMock(return_value=RevenueCatEntitlement(
            app_user_id=str(user.id),
            product_id="kida.premium.monthly",
            status=IapSubscriptionStatus.active,
            expires_at=exp,
            platform=None,
            store_transaction_id="gpa-9",
        )),
    )
    resp = await client.post(
        "/api/v1/subscriptions/verify/revenuecat", headers=_auth_headers(user)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["product_id"] == "kida.premium.monthly"

    sub = await _get_sub(db_session, user.id)
    assert sub.source == IapSubscriptionSource.revenuecat


@pytest.mark.asyncio
async def test_verify_revenuecat_no_entitlement_is_inactive(client, db_session, monkeypatch):
    user = await _create_user(db_session)
    monkeypatch.setattr(
        RevenueCatClient, "fetch_entitlement", AsyncMock(return_value=None)
    )
    resp = await client.post(
        "/api/v1/subscriptions/verify/revenuecat", headers=_auth_headers(user)
    )
    assert resp.status_code == 200
    assert resp.json()["active"] is False


@pytest.mark.asyncio
async def test_verify_revenuecat_requires_auth(client):
    resp = await client.post("/api/v1/subscriptions/verify/revenuecat")
    assert resp.status_code == 403
