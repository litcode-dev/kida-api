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


def _purchase_event(**event):
    """An INITIAL_PURCHASE shaped like a real Play Store delivery."""
    return {"api_version": "1.0", "event": {
        "type": "INITIAL_PURCHASE",
        "product_id": "kida.premium.monthly:monthly",
        "entitlement_ids": ["Kida Pro"],
        "expiration_at_ms": _future_ms(30),
        "store": "PLAY_STORE",
        "original_transaction_id": f"GPA.{uuid.uuid4()}",
        **event,
    }}


@pytest.mark.asyncio
async def test_webhook_maps_email_app_user_id(client, db_session, monkeypatch):
    # RevenueCat customers created before the app logged in with the user's id
    # carry the account email as their app_user_id.
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "Kida Pro")
    user = await _create_user(db_session)
    rc_id = user.email.upper()

    resp = await client.post(
        "/api/v1/subscriptions/webhook/revenuecat",
        headers={"Authorization": "hook-secret"},
        json=_purchase_event(
            app_user_id=rc_id,
            original_app_user_id="$RCAnonymousID:17dd35d141304f46a820fdf174112f90",
            aliases=["$RCAnonymousID:17dd35d141304f46a820fdf174112f90", rc_id],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is True

    sub = await _get_sub(db_session, user.id)
    assert sub.status == IapSubscriptionStatus.active
    assert sub.product_id == "kida.premium.monthly:monthly"
    # Kept as RevenueCat knows it, so account deletion reaches that customer.
    assert sub.app_user_id == rc_id


@pytest.mark.asyncio
async def test_webhook_maps_user_id_found_in_aliases(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "Kida Pro")
    user = await _create_user(db_session)
    other = await _create_user(db_session)

    resp = await client.post(
        "/api/v1/subscriptions/webhook/revenuecat",
        headers={"Authorization": "hook-secret"},
        json=_purchase_event(
            app_user_id="$RCAnonymousID:abc",
            # An id beats an email: logIn(user.id) is the binding the app makes.
            aliases=["$RCAnonymousID:abc", other.email, str(user.id)],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    assert (await _get_sub(db_session, user.id)).status == IapSubscriptionStatus.active
    assert await _get_sub(db_session, other.id) is None


@pytest.mark.asyncio
async def test_webhook_unknown_identities_no_op(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "Kida Pro")
    resp = await client.post(
        "/api/v1/subscriptions/webhook/revenuecat",
        headers={"Authorization": "hook-secret"},
        json=_purchase_event(
            app_user_id="nobody@example.com",
            aliases=["nobody@example.com", str(uuid.uuid4())],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is False


def _ms(dt):
    return int(dt.timestamp() * 1000)


async def _post_event(client, **event):
    resp = await client.post(
        "/api/v1/subscriptions/webhook/revenuecat",
        headers={"Authorization": "hook-secret"},
        json={"api_version": "1.0", "event": event},
    )
    assert resp.status_code == 200
    return resp


@pytest.mark.asyncio
async def test_webhook_billing_cancellation_keeps_grace_window(client, db_session, monkeypatch):
    # Play delivers BILLING_ISSUE and CANCELLATION(BILLING_ERROR) milliseconds
    # apart. Only BILLING_ISSUE carries the grace expiry; the cancellation that
    # follows must not cut the grace window back to the paid-period expiry.
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "Kida Pro")
    user = await _create_user(db_session)
    now = datetime.now(timezone.utc)
    paid_until = now + timedelta(days=4)
    grace_until = paid_until + timedelta(hours=2)
    common = {
        "app_user_id": user.email,
        "product_id": "kida.premium.monthly:monthly",
        "entitlement_ids": ["Kida Pro"],
        "store": "PLAY_STORE",
        "original_transaction_id": "GPA.billing-1",
        "expiration_at_ms": _ms(paid_until),
    }

    await _post_event(
        client, **common, type="BILLING_ISSUE",
        grace_period_expiration_at_ms=_ms(grace_until),
        event_timestamp_ms=_ms(now),
    )
    await _post_event(
        client, **common, type="CANCELLATION", cancel_reason="BILLING_ERROR",
        event_timestamp_ms=_ms(now) + 6,
    )

    sub = await _get_sub(db_session, user.id)
    await db_session.refresh(sub)
    assert sub.status == IapSubscriptionStatus.grace
    assert _ms(sub.expires_at) == _ms(grace_until)


@pytest.mark.asyncio
async def test_webhook_ignores_event_older_than_last_applied(client, db_session, monkeypatch):
    # RevenueCat does not guarantee delivery order; a late, older RENEWAL must
    # not resurrect an entitlement a newer EXPIRATION already ended.
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "hook-secret")
    user = await _create_user(db_session)
    now = datetime.now(timezone.utc)
    common = {
        "app_user_id": str(user.id),
        "product_id": "kida.premium.monthly",
        "store": "APP_STORE",
        "original_transaction_id": "orig-stale",
    }

    await _post_event(
        client, **common, type="EXPIRATION",
        expiration_at_ms=_ms(now - timedelta(hours=1)),
        event_timestamp_ms=_ms(now),
    )
    await _post_event(
        client, **common, type="RENEWAL",
        expiration_at_ms=_ms(now + timedelta(days=30)),
        event_timestamp_ms=_ms(now - timedelta(days=1)),
    )

    sub = await _get_sub(db_session, user.id)
    await db_session.refresh(sub)
    assert sub.status == IapSubscriptionStatus.expired


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
async def test_verify_revenuecat_falls_back_to_email(client, db_session, monkeypatch):
    user = await _create_user(db_session)
    entitlement = RevenueCatEntitlement(
        app_user_id=user.email,
        product_id="kida.premium.monthly:monthly",
        status=IapSubscriptionStatus.active,
        expires_at=datetime.now(timezone.utc) + timedelta(days=20),
        platform=None,
        store_transaction_id="gpa-email",
    )
    fetch = AsyncMock(side_effect=lambda rc_id: entitlement if rc_id == user.email else None)
    monkeypatch.setattr(RevenueCatClient, "fetch_entitlement", fetch)

    resp = await client.post(
        "/api/v1/subscriptions/verify/revenuecat", headers=_auth_headers(user)
    )
    assert resp.status_code == 200
    assert resp.json()["active"] is True
    assert [c.args[0] for c in fetch.await_args_list] == [str(user.id), user.email]
    assert (await _get_sub(db_session, user.id)).app_user_id == user.email


@pytest.mark.asyncio
async def test_verify_revenuecat_requires_auth(client):
    resp = await client.post("/api/v1/subscriptions/verify/revenuecat")
    assert resp.status_code == 403
