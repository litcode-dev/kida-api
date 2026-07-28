import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.exceptions import AppError
from app.models.iap_subscription import (
    IapSubscription,
    IapSubscriptionSource,
    IapSubscriptionStatus,
    PremiumPlan,
)
from app.models.user import User, UserRole
from app.services import iap_subscription_service
from app.services.auth_service import hash_password


async def _create_user(db):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Test",
        role=UserRole.user,
    )
    db.add(user)
    await db.commit()
    return user


async def _add_store_sub(db, user_id, product_id="kida.premium.monthly"):
    sub = IapSubscription(
        user_id=user_id,
        platform="android",
        product_id=product_id,
        store_transaction_id=str(uuid.uuid4()),
        status=IapSubscriptionStatus.active,
        expires_at=datetime.now(timezone.utc) + timedelta(days=20),
    )
    db.add(sub)
    await db.commit()
    return sub


@pytest.mark.asyncio
async def test_admin_activate_creates_monthly_entitlement(db_session):
    user = await _create_user(db_session)

    sub = await iap_subscription_service.admin_activate(
        db_session, user.id, PremiumPlan.monthly
    )

    assert iap_subscription_service.is_active(sub) is True
    assert sub.product_id == "kida.premium.monthly"
    assert sub.source is IapSubscriptionSource.admin
    assert sub.platform is None
    assert sub.store_transaction_id.startswith("admin:")
    days = (sub.expires_at - datetime.now(timezone.utc)).days
    assert 28 <= days <= 30


@pytest.mark.asyncio
async def test_admin_activate_yearly_uses_year_long_period(db_session):
    user = await _create_user(db_session)

    sub = await iap_subscription_service.admin_activate(
        db_session, user.id, PremiumPlan.yearly
    )

    assert sub.product_id == "kida.premium.yearly"
    days = (sub.expires_at - datetime.now(timezone.utc)).days
    assert 363 <= days <= 365


@pytest.mark.asyncio
async def test_admin_activate_honours_custom_expiry(db_session):
    user = await _create_user(db_session)
    expires = datetime.now(timezone.utc) + timedelta(days=3)

    sub = await iap_subscription_service.admin_activate(
        db_session, user.id, PremiumPlan.monthly, expires_at=expires
    )

    assert abs((sub.expires_at - expires).total_seconds()) < 1


@pytest.mark.asyncio
async def test_admin_activate_rejects_past_expiry(db_session):
    user = await _create_user(db_session)
    past = datetime.now(timezone.utc) - timedelta(days=1)

    with pytest.raises(AppError, match="future"):
        await iap_subscription_service.admin_activate(
            db_session, user.id, PremiumPlan.monthly, expires_at=past
        )


@pytest.mark.asyncio
async def test_admin_activate_switches_existing_plan_in_place(db_session):
    user = await _create_user(db_session)
    store_sub = await _add_store_sub(db_session, user.id)
    original_transaction_id = store_sub.store_transaction_id

    sub = await iap_subscription_service.admin_activate(
        db_session, user.id, PremiumPlan.yearly
    )

    assert sub.id == store_sub.id
    assert sub.product_id == "kida.premium.yearly"
    # The store purchase stays traceable even after an admin takes the plan over.
    assert sub.store_transaction_id == original_transaction_id


@pytest.mark.asyncio
async def test_admin_deactivate_revokes_entitlement(db_session):
    user = await _create_user(db_session)
    await _add_store_sub(db_session, user.id)

    sub = await iap_subscription_service.admin_deactivate(
        db_session, user.id, note="Refund issued"
    )

    assert sub.status is IapSubscriptionStatus.revoked
    assert sub.admin_locked is True
    assert sub.admin_note == "Refund issued"
    assert iap_subscription_service.is_active(sub) is False


@pytest.mark.asyncio
async def test_admin_deactivate_without_entitlement_returns_none(db_session):
    user = await _create_user(db_session)
    assert await iap_subscription_service.admin_deactivate(db_session, user.id) is None


@pytest.mark.asyncio
async def test_receipt_verification_cannot_restore_deactivated_entitlement(db_session):
    user = await _create_user(db_session)
    await _add_store_sub(db_session, user.id)
    await iap_subscription_service.admin_deactivate(db_session, user.id)

    with pytest.raises(AppError, match="administrator"):
        await iap_subscription_service.verify_and_upsert(
            db_session, user.id, "kida.premium.monthly", "android", "receipt"
        )

    sub = await iap_subscription_service.get_subscription(db_session, user.id)
    assert iap_subscription_service.is_active(sub) is False


@pytest.mark.asyncio
async def test_admin_activate_clears_earlier_deactivation(db_session):
    user = await _create_user(db_session)
    await _add_store_sub(db_session, user.id)
    await iap_subscription_service.admin_deactivate(db_session, user.id)

    sub = await iap_subscription_service.admin_activate(
        db_session, user.id, PremiumPlan.monthly
    )
    assert sub.admin_locked is False
    assert iap_subscription_service.is_active(sub) is True

    # ...and the app can verify its receipt again.
    sub = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.monthly", "android", "receipt"
    )
    assert iap_subscription_service.is_active(sub) is True


@pytest.mark.asyncio
async def test_store_purchase_takes_over_an_admin_grant(db_session):
    user = await _create_user(db_session)
    await iap_subscription_service.admin_activate(db_session, user.id, PremiumPlan.monthly)

    sub = await iap_subscription_service.verify_and_upsert(
        db_session, user.id, "kida.premium.yearly", "ios", "receipt"
    )

    assert sub.source is IapSubscriptionSource.store
    assert sub.product_id == "kida.premium.yearly"


def test_plan_for_product():
    assert iap_subscription_service.plan_for_product("kida.premium.yearly") is PremiumPlan.yearly
    assert iap_subscription_service.plan_for_product("kida.premium.monthly") is PremiumPlan.monthly
    assert iap_subscription_service.plan_for_product(None) is None


def test_admin_view_without_entitlement():
    view = iap_subscription_service.admin_view(uuid.uuid4(), None)
    assert view["active"] is False
    assert view["plan"] is None
    assert view["admin_locked"] is False
