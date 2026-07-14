import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models.iap_subscription import IapSubscription, IapSubscriptionStatus
from app.models.user import User, UserRole
from app.services.auth_service import hash_password
from app.services import iap_subscription_service
from app.services.iap_types import VerifiedSubscription


async def _create_user(db):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"), full_name="Test",
        role=UserRole.user,
    )
    db.add(user)
    await db.commit()
    return user


class _FakeClient:
    """Stand-in for PlaySubscriptions / AppStoreSubscriptions."""

    def __init__(self, verified: VerifiedSubscription):
        self._verified = verified
        self.calls = 0

    async def verify(self, receipt: str) -> VerifiedSubscription:
        self.calls += 1
        return self._verified


def _future(days=30):
    return datetime.now(timezone.utc) + timedelta(days=days)


# ---- verify_and_upsert (mocked store clients) ----

@pytest.mark.asyncio
async def test_android_verify_creates_active_row(db_session):
    user = await _create_user(db_session)
    verified = VerifiedSubscription(
        store_transaction_id="play-token-1",
        product_id="kida.premium.monthly",
        expires_at=_future(),
        status="active",
        latest_receipt="{...}",
        raw_payload={"subscriptionState": "SUBSCRIPTION_STATE_ACTIVE"},
    )
    client = _FakeClient(verified)
    sub = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.monthly", "android", "{...}", play_client=client,
    )
    assert sub.status == IapSubscriptionStatus.active
    assert sub.store_transaction_id == "play-token-1"
    assert sub.product_id == "kida.premium.monthly"
    assert iap_subscription_service.is_active(sub) is True
    assert client.calls == 1


@pytest.mark.asyncio
async def test_ios_verify_creates_active_row(db_session):
    user = await _create_user(db_session)
    verified = VerifiedSubscription(
        store_transaction_id="orig-txn-1",
        product_id="kida.premium.yearly",
        expires_at=_future(365),
        status="active",
        latest_receipt="b64",
        raw_payload={"status": 0},
    )
    client = _FakeClient(verified)
    sub = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.yearly", "ios", "b64", app_store_client=client,
    )
    assert sub.platform.value == "ios"
    assert sub.store_transaction_id == "orig-txn-1"
    assert iap_subscription_service.is_active(sub) is True


@pytest.mark.asyncio
async def test_idempotent_reverify_updates_same_row(db_session):
    user = await _create_user(db_session)
    verified = VerifiedSubscription(
        store_transaction_id="play-token-1",
        product_id="kida.premium.monthly",
        expires_at=_future(),
        status="active",
        latest_receipt="{...}",
        raw_payload={},
    )
    client = _FakeClient(verified)
    first = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.monthly", "android", "{...}", play_client=client,
    )
    second = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.monthly", "android", "{...}", play_client=client,
    )
    assert first.id == second.id
    count = await db_session.scalar(
        select(func.count()).select_from(IapSubscription).where(IapSubscription.user_id == user.id)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_switching_plan_updates_same_row(db_session):
    user = await _create_user(db_session)
    monthly = VerifiedSubscription(
        store_transaction_id="token-monthly", product_id="kida.premium.monthly",
        expires_at=_future(), status="active", latest_receipt="{...}", raw_payload={},
    )
    yearly = VerifiedSubscription(
        store_transaction_id="token-yearly", product_id="kida.premium.yearly",
        expires_at=_future(365), status="active", latest_receipt="{...}", raw_payload={},
    )
    await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.monthly", "android", "{...}", play_client=_FakeClient(monthly),
    )
    sub = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.yearly", "android", "{...}", play_client=_FakeClient(yearly),
    )
    assert sub.product_id == "kida.premium.yearly"
    assert sub.store_transaction_id == "token-yearly"
    count = await db_session.scalar(
        select(func.count()).select_from(IapSubscription).where(IapSubscription.user_id == user.id)
    )
    assert count == 1


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
