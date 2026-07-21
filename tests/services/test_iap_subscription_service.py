import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models.iap_subscription import IapSubscription, IapSubscriptionStatus
from app.models.user import User, UserRole
from app.services.auth_service import hash_password
from app.services import iap_subscription_service


async def _create_user(db):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"), full_name="Test",
        role=UserRole.user,
    )
    db.add(user)
    await db.commit()
    return user


def _future(days=30):
    return datetime.now(timezone.utc) + timedelta(days=days)


# ---- verify_and_upsert (mocked store clients) ----

# ---- verify_and_upsert (store-side verification disabled — trusts the client) ----

@pytest.mark.asyncio
async def test_android_verify_creates_active_row(db_session):
    user = await _create_user(db_session)
    sub = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.monthly", "android", "{...}",
    )
    assert sub.status == IapSubscriptionStatus.active
    assert sub.store_transaction_id == hashlib.sha256(b"{...}").hexdigest()
    assert sub.product_id == "kida.premium.monthly"
    assert iap_subscription_service.is_active(sub) is True
    # Monthly product ids get a ~30 day assumed billing period.
    assert 29 <= (sub.expires_at - datetime.now(timezone.utc)).days <= 30


@pytest.mark.asyncio
async def test_ios_verify_creates_active_row(db_session):
    user = await _create_user(db_session)
    sub = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.yearly", "ios", "b64",
    )
    assert sub.platform.value == "ios"
    assert sub.store_transaction_id == hashlib.sha256(b"b64").hexdigest()
    assert iap_subscription_service.is_active(sub) is True
    # Yearly product ids get a ~365 day assumed billing period.
    assert 364 <= (sub.expires_at - datetime.now(timezone.utc)).days <= 365


@pytest.mark.asyncio
async def test_idempotent_reverify_updates_same_row(db_session):
    user = await _create_user(db_session)
    first = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.monthly", "android", "{...}",
    )
    second = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.monthly", "android", "{...}",
    )
    assert first.id == second.id
    count = await db_session.scalar(
        select(func.count()).select_from(IapSubscription).where(IapSubscription.user_id == user.id)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_switching_plan_updates_same_row(db_session):
    user = await _create_user(db_session)
    await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.monthly", "android", "{...monthly...}",
    )
    sub = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.yearly", "android", "{...yearly...}",
    )
    assert sub.product_id == "kida.premium.yearly"
    assert sub.store_transaction_id == hashlib.sha256(b"{...yearly...}").hexdigest()
    count = await db_session.scalar(
        select(func.count()).select_from(IapSubscription).where(IapSubscription.user_id == user.id)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_rejects_bad_platform(db_session):
    user = await _create_user(db_session)
    with pytest.raises(Exception):
        await iap_subscription_service.verify_and_upsert(
            db_session, user.id, "kida.premium.monthly", "windows", "{...}",
        )


# ---- entitlement / is_active ----

def _row(status, expires_at):
    return IapSubscription(
        user_id=uuid.uuid4(), platform="android", product_id="kida.premium.monthly",
        store_transaction_id=str(uuid.uuid4()), status=status, expires_at=expires_at,
    )


def test_is_active_true_when_active_and_future():
    assert iap_subscription_service.is_active(_row(IapSubscriptionStatus.active, _future())) is True


def test_is_active_true_in_grace_period():
    assert iap_subscription_service.is_active(_row(IapSubscriptionStatus.grace, _future(3))) is True


def test_is_active_false_when_expired():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert iap_subscription_service.is_active(_row(IapSubscriptionStatus.active, past)) is False
    assert iap_subscription_service.is_active(_row(IapSubscriptionStatus.expired, _future())) is False


def test_is_active_false_when_none():
    assert iap_subscription_service.is_active(None) is False


def test_entitlement_shape_none():
    ent = iap_subscription_service.entitlement(None)
    assert ent == {"active": False, "expires_at": None, "product_id": None}


def test_entitlement_shape_active():
    exp = _future()
    ent = iap_subscription_service.entitlement(_row(IapSubscriptionStatus.active, exp))
    assert ent["active"] is True
    assert ent["product_id"] == "kida.premium.monthly"
    assert ent["expires_at"] == exp.isoformat()
