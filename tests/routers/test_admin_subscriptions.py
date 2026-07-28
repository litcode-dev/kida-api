import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.iap_subscription import IapSubscription, IapSubscriptionStatus
from app.models.purchase import PaymentProvider
from app.models.subscription import Subscription, SubscriptionPlan, SubscriptionStatus
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


async def _make_user(db, role=UserRole.user):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Test",
        role=role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _add_sub(db, user_id, product_id="kida.premium.monthly"):
    sub = IapSubscription(
        user_id=user_id,
        platform="android",
        product_id=product_id,
        store_transaction_id=str(uuid.uuid4()),
        status=IapSubscriptionStatus.active,
        expires_at=datetime.now(timezone.utc) + timedelta(days=10),
    )
    db.add(sub)
    await db.commit()
    return sub


@pytest.mark.asyncio
async def test_activate_monthly_plan(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/activate",
        json={"plan": "monthly"},
        headers=_headers(admin),
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["active"] is True
    assert data["plan"] == "monthly"
    assert data["source"] == "admin"
    assert data["product_id"] == "kida.premium.monthly"


@pytest.mark.asyncio
async def test_activate_yearly_plan(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/activate",
        json={"plan": "yearly", "note": "Launch partner comp"},
        headers=_headers(admin),
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["plan"] == "yearly"
    assert data["admin_note"] == "Launch partner comp"
    expires = datetime.fromisoformat(data["expires_at"])
    assert (expires - datetime.now(timezone.utc)).days >= 363


@pytest.mark.asyncio
async def test_activate_rejects_unknown_plan(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/activate",
        json={"plan": "weekly"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_activate_rejects_past_expiry(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/activate",
        json={"plan": "monthly", "expires_at": past},
        headers=_headers(admin),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_deactivate_revokes_entitlement(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)
    await _add_sub(db_session, target.id)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/deactivate",
        json={"note": "Chargeback"},
        headers=_headers(admin),
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["active"] is False
    assert data["status"] == "revoked"
    assert data["admin_locked"] is True


@pytest.mark.asyncio
async def test_deactivate_without_body(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)
    await _add_sub(db_session, target.id)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/deactivate",
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["active"] is False


@pytest.mark.asyncio
async def test_deactivate_expires_web_subscription(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(
        Subscription(
            user_id=target.id,
            plan=SubscriptionPlan.premium,
            status=SubscriptionStatus.active,
            provider=PaymentProvider.paystack,
            payment_reference=str(uuid.uuid4()),
            amount_paid=Decimal("2500.00"),
            billing_period_start=now,
            expires_at=now + timedelta(days=30),
        )
    )
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/deactivate",
        headers=_headers(admin),
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["web_subscription_expired"] is True

    from app.services import subscription_service
    assert await subscription_service.get_active_subscription(db_session, target.id) is None


@pytest.mark.asyncio
async def test_deactivated_user_loses_entitlement_on_me(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)
    await _add_sub(db_session, target.id)

    await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/deactivate",
        headers=_headers(admin),
    )

    resp = await client.get("/api/v1/subscriptions/me", headers=_headers(target))
    assert resp.status_code == 200
    assert resp.json()["active"] is False


@pytest.mark.asyncio
async def test_deactivated_user_cannot_reverify_receipt(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)
    await _add_sub(db_session, target.id)

    await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/deactivate",
        headers=_headers(admin),
    )

    resp = await client.post(
        "/api/v1/subscriptions/verify",
        json={"product_id": "kida.premium.monthly", "platform": "android", "receipt": "r"},
        headers=_headers(target),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_subscription_returns_entitlement(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)
    await _add_sub(db_session, target.id, "kida.premium.yearly")

    resp = await client.get(
        f"/api/v1/admin/users/{target.id}/subscription", headers=_headers(admin)
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["active"] is True
    assert data["plan"] == "yearly"
    assert data["source"] == "store"


@pytest.mark.asyncio
async def test_get_subscription_for_user_without_one(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)

    resp = await client.get(
        f"/api/v1/admin/users/{target.id}/subscription", headers=_headers(admin)
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["active"] is False
    assert data["plan"] is None


@pytest.mark.asyncio
async def test_non_admin_cannot_activate(client, db_session):
    user = await _make_user(db_session)
    target = await _make_user(db_session)

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/activate",
        json={"plan": "monthly"},
        headers=_headers(user),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_activate_requires_auth(client, db_session):
    target = await _make_user(db_session)
    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/activate",
        json={"plan": "monthly"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_activate_unknown_user_returns_404(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)

    resp = await client.put(
        f"/api/v1/admin/users/{uuid.uuid4()}/subscription/activate",
        json={"plan": "monthly"},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_user_list_includes_subscription(client, db_session):
    admin = await _make_user(db_session, UserRole.admin)
    subscriber = await _make_user(db_session)
    await _add_sub(db_session, subscriber.id, "kida.premium.yearly")
    await _make_user(db_session)  # never subscribed

    resp = await client.get("/api/v1/admin/users?page_size=50", headers=_headers(admin))

    assert resp.status_code == 200
    items = {i["id"]: i for i in resp.json()["data"]["items"]}
    assert items[str(subscriber.id)]["subscription"]["plan"] == "yearly"
    assert items[str(subscriber.id)]["subscription"]["active"] is True
    # Everyone gets the key, so the client never has to special-case its absence.
    assert items[str(admin.id)]["subscription"]["active"] is False
    assert items[str(admin.id)]["subscription"]["plan"] is None


@pytest.mark.asyncio
async def test_user_list_does_not_issue_a_query_per_user(client, db_session):
    """The subscription lookup must stay one query no matter how many users."""
    from sqlalchemy import event
    from tests.conftest import test_engine

    admin = await _make_user(db_session, UserRole.admin)
    for _ in range(5):
        user = await _make_user(db_session)
        await _add_sub(db_session, user.id)

    sub_queries = []

    def count(conn, cursor, statement, parameters, context, executemany):
        if "iap_subscriptions" in statement and statement.lstrip().upper().startswith("SELECT"):
            sub_queries.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count)
    try:
        resp = await client.get("/api/v1/admin/users?page_size=50", headers=_headers(admin))
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count)

    assert resp.status_code == 200
    assert len(resp.json()["data"]["items"]) >= 6
    assert len(sub_queries) == 1, f"expected 1 subscription query, got {len(sub_queries)}"


@pytest.mark.asyncio
async def test_activated_user_is_exempt_from_free_tier_caps(client, db_session):
    from app.services import free_tier_service

    admin = await _make_user(db_session, UserRole.admin)
    target = await _make_user(db_session)
    assert await free_tier_service.is_subscribed(db_session, target.id) is False

    await client.put(
        f"/api/v1/admin/users/{target.id}/subscription/activate",
        json={"plan": "monthly"},
        headers=_headers(admin),
    )

    assert await free_tier_service.is_subscribed(db_session, target.id) is True
