import pytest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from app.models.user import User, UserRole
from app.models.subscription import Subscription, SubscriptionPlan, SubscriptionStatus
from app.models.purchase import PaymentProvider
from app.services.auth_service import hash_password, create_access_token


async def _create_user(db, ai_enabled=True, ai_extra_credits=0):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"), full_name="Test",
        role=UserRole.user, ai_enabled=ai_enabled, ai_extra_credits=ai_extra_credits,
    )
    db.add(user)
    await db.commit()
    return user


async def _create_active_sub(db, user_id, quota_used=0):
    now = datetime.now(timezone.utc)
    sub = Subscription(
        id=uuid.uuid4(), user_id=user_id, plan=SubscriptionPlan.premium,
        status=SubscriptionStatus.active, provider=PaymentProvider.paystack,
        payment_reference=f"ref-{uuid.uuid4().hex[:8]}", amount_paid=Decimal("2000"),
        ai_quota=10, ai_quota_used=quota_used,
        billing_period_start=now, expires_at=now + timedelta(days=30),
    )
    db.add(sub)
    await db.commit()
    return sub


def _auth_headers(user):
    token = create_access_token(str(user.id), user.role.value)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_generate_requires_auth(client):
    resp = await client.post("/api/v1/ai/generate", json={"prompt": "test", "provider": "suno"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_generate_blocked_when_ai_disabled(client, db_session):
    user = await _create_user(db_session, ai_enabled=False)
    resp = await client.post(
        "/api/v1/ai/generate",
        json={"prompt": "test", "provider": "suno"},
        headers=_auth_headers(user),
    )
    assert resp.status_code == 403
    assert "disabled" in resp.json()["message"].lower()


@pytest.mark.asyncio
async def test_generate_blocked_without_subscription(client, db_session):
    user = await _create_user(db_session)
    resp = await client.post(
        "/api/v1/ai/generate",
        json={"prompt": "test", "provider": "suno"},
        headers=_auth_headers(user),
    )
    assert resp.status_code == 402


@pytest.mark.asyncio
async def test_generate_blocked_when_quota_exhausted_no_extras(client, db_session):
    user = await _create_user(db_session, ai_extra_credits=0)
    await _create_active_sub(db_session, user.id, quota_used=10)
    resp = await client.post(
        "/api/v1/ai/generate",
        json={"prompt": "test", "provider": "suno"},
        headers=_auth_headers(user),
    )
    assert resp.status_code == 402
    assert "extra credits" in resp.json()["message"].lower()


@pytest.mark.asyncio
async def test_generate_succeeds_with_active_subscription(client, db_session, monkeypatch):
    from app.tasks import ai_tasks
    monkeypatch.setattr(ai_tasks.generate_ai_loop_task, "delay", lambda *a, **kw: None)

    user = await _create_user(db_session)
    await _create_active_sub(db_session, user.id, quota_used=0)

    resp = await client.post(
        "/api/v1/ai/generate",
        json={"prompt": "chill afrobeat loop", "provider": "suno"},
        headers=_auth_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "pending"


@pytest.mark.asyncio
async def test_generate_uses_extra_credits_when_quota_full(client, db_session, monkeypatch):
    from app.tasks import ai_tasks
    monkeypatch.setattr(ai_tasks.generate_ai_loop_task, "delay", lambda *a, **kw: None)

    user = await _create_user(db_session, ai_extra_credits=2)
    await _create_active_sub(db_session, user.id, quota_used=10)

    resp = await client.post(
        "/api/v1/ai/generate",
        json={"prompt": "trap loop", "provider": "self_hosted"},
        headers=_auth_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["is_extra"] is True
    await db_session.refresh(user)
    assert user.ai_extra_credits == 1


@pytest.mark.asyncio
async def test_admin_can_disable_user_ai(client, db_session):
    admin_user = await _create_user(db_session)
    admin_user.role = UserRole.admin
    await db_session.commit()

    target = await _create_user(db_session)
    assert target.ai_enabled is True

    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/ai-enabled",
        params={"enabled": "false"},
        headers=_auth_headers(admin_user),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["ai_enabled"] is False
    await db_session.refresh(target)
    assert target.ai_enabled is False
