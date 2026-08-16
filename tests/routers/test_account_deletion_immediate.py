"""Deleting an account takes effect at once — there is no grace period.

What used to be a soft delete plus a 30-day window is now a single hard delete.
These cover the consequences: the row is gone before the response arrives, no
credential brings it back, the address is immediately free again, and the
internal notification fires now rather than a month later.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.models.user import User, UserRole
from app.services import auth_service
from app.services.auth_service import (
    REFRESH_PREFIX,
    VERIFY_PREFIX,
    create_access_token,
    hash_password,
)

PASSWORD = "pass1234"
EMAIL = "leaver@test.com"


async def _make_user(db, email=EMAIL, verified=True):
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=await hash_password(PASSWORD),
        full_name="Leaver",
        role=UserRole.user,
        is_verified=verified,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _delete_account(client, user, refresh_token=None):
    body = {"refresh_token": refresh_token} if refresh_token else {}
    with patch("app.services.email_service.send_email", new=AsyncMock()), \
         patch("app.services.s3_service.delete_object", new=AsyncMock()), \
         patch(
             "app.tasks.notification_tasks.send_account_deleted_admin_notification.delay",
             new=lambda *a, **kw: None,
         ), \
         patch(
             "app.tasks.deletion_tasks.propagate_account_deletion.delay",
             new=lambda *a, **kw: None,
         ):
        return await client.request(
            "DELETE", "/api/v1/auth/me", headers=_headers(user), json=body,
        )


async def _login(client, email=EMAIL, password=PASSWORD):
    return await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )


# ── The row is gone straight away ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_account_removes_the_row_immediately(client, db_session):
    user = await _make_user(db_session)
    uid = user.id

    resp = await _delete_account(client, user)

    assert resp.status_code == 200
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == uid)
    ) == 0


@pytest.mark.asyncio
async def test_the_response_promises_nothing_recoverable(client, db_session):
    """The old body advertised purge_due_at/grace_days/restorable. Nothing to say now."""
    user = await _make_user(db_session)

    resp = await _delete_account(client, user)

    assert resp.json().get("data") in (None, {})
    assert "delet" in resp.json()["message"].lower()


@pytest.mark.asyncio
async def test_the_access_token_stops_working(client, db_session):
    user = await _make_user(db_session)
    headers = _headers(user)

    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    await _delete_account(client, user)

    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_every_session_is_revoked_not_just_the_one_that_asked(
    client, db_session, fake_redis
):
    user = await _make_user(db_session)
    await auth_service.store_refresh_token(fake_redis, "tok-phone", str(user.id))
    await auth_service.store_refresh_token(fake_redis, "tok-tablet", str(user.id))

    await _delete_account(client, user, refresh_token="tok-phone")

    assert await fake_redis.get(f"{REFRESH_PREFIX}tok-phone") is None
    assert await fake_redis.get(f"{REFRESH_PREFIX}tok-tablet") is None


@pytest.mark.asyncio
async def test_a_refresh_token_cannot_outlive_the_account(client, db_session):
    user = await _make_user(db_session)
    login = await _login(client)
    refresh_token = login.json()["data"]["refresh_token"]

    await _delete_account(client, user)

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_deleting_twice_is_refused_because_there_is_nothing_left(
    client, db_session
):
    """The second call authenticates an account that no longer exists."""
    user = await _make_user(db_session)

    first = await _delete_account(client, user)
    second = await _delete_account(client, user)

    assert first.status_code == 200
    assert second.status_code == 401


# ── Nothing brings it back ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_password_no_longer_signs_anyone_in(client, db_session):
    user = await _make_user(db_session)
    await _delete_account(client, user)

    assert (await _login(client)).status_code == 401


@pytest.mark.asyncio
async def test_oauth_sign_in_creates_a_new_account_not_the_old_one(client, db_session):
    """With the row gone there is nothing left to link to — Google sees a stranger."""
    user = await _make_user(db_session, email="oauth@test.com")
    user.oauth_provider = "google"
    user.oauth_provider_id = "google-123"
    await db_session.commit()
    old_id = user.id

    await _delete_account(client, user)

    with patch("app.tasks.notification_tasks.send_registration_email.delay"), \
         patch("app.tasks.notification_tasks.send_new_user_admin_notification.delay"):
        found = await auth_service.find_or_create_oauth_user(
            db_session, email="oauth@test.com", full_name="OAuth User",
            provider="google", provider_id="google-123",
        )

    assert found.id != old_id


@pytest.mark.asyncio
async def test_the_address_is_free_to_register_again_at_once(client, db_session):
    user = await _make_user(db_session)
    await _delete_account(client, user)

    resp = await client.post("/api/v1/auth/register", json={
        "email": EMAIL, "password": PASSWORD, "full_name": "Someone New",
    })

    assert resp.status_code == 200
    assert resp.json()["data"]["id"] != str(user.id)


@pytest.mark.asyncio
async def test_re_registering_is_a_genuinely_new_signup(client, db_session, fake_redis):
    """No returning-user branch any more: the welcome mail and the admin
    new-signup notification both fire, because the account really is new."""
    from app.tasks import notification_tasks

    user = await _make_user(db_session)
    await _delete_account(client, user)

    reg = await client.post("/api/v1/auth/register", json={
        "email": EMAIL, "password": PASSWORD, "full_name": "Someone New",
    })
    new_id = reg.json()["data"]["id"]
    code = fake_redis.store[f"{VERIFY_PREFIX}{new_id}"]

    queued = []
    with patch.object(
        notification_tasks.send_registration_email, "delay",
        lambda *a, **kw: queued.append("welcome"),
    ), patch.object(
        notification_tasks.send_new_user_admin_notification, "delay",
        lambda *a, **kw: queued.append("new_signup_admin"),
    ):
        resp = await client.post("/api/v1/auth/verify-email", json={
            "email": EMAIL, "code": code,
        })

    assert resp.status_code == 200
    assert queued == ["welcome", "new_signup_admin"]


@pytest.mark.asyncio
async def test_registering_over_a_live_account_is_still_a_conflict(client, db_session):
    await _make_user(db_session)

    resp = await client.post("/api/v1/auth/register", json={
        "email": EMAIL, "password": "another-pass", "full_name": "Impostor",
    })

    assert resp.status_code == 409


# ── The mail that goes out ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_user_is_told_the_account_is_gone_not_scheduled(client, db_session):
    user = await _make_user(db_session)

    sent = []

    async def _capture(**kw):
        sent.append(kw)

    with patch("app.services.email_service.send_email", new=_capture), \
         patch("app.services.s3_service.delete_object", new=AsyncMock()), \
         patch(
             "app.tasks.notification_tasks.send_account_deleted_admin_notification.delay",
             new=lambda *a, **kw: None,
         ), \
         patch(
             "app.tasks.deletion_tasks.propagate_account_deletion.delay",
             new=lambda *a, **kw: None,
         ):
        await client.request(
            "DELETE", "/api/v1/auth/me", headers=_headers(user), json={},
        )

    assert [s["subject"] for s in sent] == ["Your Kida account has been deleted"]


@pytest.mark.asyncio
async def test_the_admin_inbox_is_notified_at_once(client, db_session, monkeypatch):
    """It used to wait a month for the purge. There is no purge to wait for."""
    from app.tasks import notification_tasks

    user = await _make_user(db_session)
    uid = str(user.id)

    calls = []
    monkeypatch.setattr(
        notification_tasks.send_account_deleted_admin_notification, "delay",
        lambda *a, **kw: calls.append(a),
    )

    with patch("app.services.email_service.send_email", new=AsyncMock()), \
         patch("app.services.s3_service.delete_object", new=AsyncMock()), \
         patch(
             "app.tasks.deletion_tasks.propagate_account_deletion.delay",
             new=lambda *a, **kw: None,
         ):
        resp = await client.request(
            "DELETE", "/api/v1/auth/me", headers=_headers(user), json={},
        )

    assert resp.status_code == 200
    assert len(calls) == 1
    sent_user_id, full_name, email, provider, joined_at, actor = calls[0]
    assert sent_user_id == uid
    assert email == EMAIL
    assert actor == "user"


# ── Mailing lists ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_deleted_account_is_not_in_a_broadcast(client, db_session):
    from app.schemas.broadcast import BroadcastAudience
    from app.services import broadcast_service

    await _make_user(db_session, email="staying@test.com")
    leaving = await _make_user(db_session, email=EMAIL)
    await _delete_account(client, leaving)

    emails = await broadcast_service.resolve_recipients(db_session, BroadcastAudience.all)

    assert emails == ["staying@test.com"]
