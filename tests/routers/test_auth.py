from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.auth_service import VERIFY_PREFIX, VERIFY_COOLDOWN_PREFIX


async def _register(client, email="new@test.com", password="securepass", full_name="Test User"):
    return await client.post("/api/v1/auth/register", json={
        "email": email,
        "password": password,
        "full_name": full_name,
    })


def _stored_code(fake_redis, user_id):
    return fake_redis.store[f"{VERIFY_PREFIX}{user_id}"]


@pytest.mark.asyncio
async def test_register_success(client):
    resp = await _register(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["data"]["email"] == "new@test.com"
    # Newly registered accounts start unverified.
    assert body["data"]["is_verified"] is False


@pytest.mark.asyncio
async def test_register_issues_verification_code(client, fake_redis):
    resp = await _register(client)
    user_id = resp.json()["data"]["id"]
    code = _stored_code(fake_redis, user_id)
    assert code.isdigit() and len(code) == 6


@pytest.mark.asyncio
async def test_register_duplicate_email(client):
    payload = {"email": "dup@test.com", "password": "pass1234", "full_name": "Dup"}
    await client.post("/api/v1/auth/register", json=payload)
    resp = await client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_login_blocked_until_verified(client):
    await _register(client, email="user@test.com", password="pass1234", full_name="User")
    resp = await client.post("/api/v1/auth/login", json={
        "email": "user@test.com", "password": "pass1234"
    })
    assert resp.status_code == 403
    body = resp.json()
    assert body["status"] == "error"
    assert body["data"] == {"is_verified": False}


@pytest.mark.asyncio
async def test_verify_email_then_login(client, fake_redis):
    reg = await _register(client, email="user@test.com", password="pass1234", full_name="User")
    user_id = reg.json()["data"]["id"]
    code = _stored_code(fake_redis, user_id)

    verify = await client.post("/api/v1/auth/verify-email", json={
        "email": "user@test.com", "code": code,
    })
    assert verify.status_code == 200
    data = verify.json()["data"]
    assert "access_token" in data
    assert "refresh_token" in data
    # Code is consumed on success.
    assert f"{VERIFY_PREFIX}{user_id}" not in fake_redis.store

    resp = await client.post("/api/v1/auth/login", json={
        "email": "user@test.com", "password": "pass1234"
    })
    assert resp.status_code == 200
    login_data = resp.json()["data"]
    assert "access_token" in login_data
    assert "refresh_token" in login_data


@pytest.mark.asyncio
async def test_verify_email_wrong_code(client):
    await _register(client, email="user@test.com", password="pass1234", full_name="User")
    resp = await client.post("/api/v1/auth/verify-email", json={
        "email": "user@test.com", "code": "000000",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_verify_email_invalid_format(client):
    await _register(client, email="user@test.com", password="pass1234", full_name="User")
    resp = await client.post("/api/v1/auth/verify-email", json={
        "email": "user@test.com", "code": "abc",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_resend_verification(client, fake_redis):
    reg = await _register(client, email="user@test.com", password="pass1234", full_name="User")
    user_id = reg.json()["data"]["id"]
    original = _stored_code(fake_redis, user_id)
    # Clear the cooldown so a fresh code can be issued immediately.
    fake_redis.store.clear()

    resp = await client.post("/api/v1/auth/resend-verification", json={"email": "user@test.com"})
    assert resp.status_code == 200
    new_code = _stored_code(fake_redis, user_id)
    assert new_code.isdigit() and len(new_code) == 6


@pytest.mark.asyncio
async def test_resend_verification_cooldown(client, fake_redis):
    reg = await _register(client, email="user@test.com", password="pass1234", full_name="User")
    user_id = reg.json()["data"]["id"]
    # Registration starts the cooldown; expire it so the first resend succeeds.
    fake_redis.store.pop(f"{VERIFY_COOLDOWN_PREFIX}{user_id}", None)
    first = await client.post("/api/v1/auth/resend-verification", json={"email": "user@test.com"})
    assert first.status_code == 200
    second = await client.post("/api/v1/auth/resend-verification", json={"email": "user@test.com"})
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_login_unverified_resends_code(client, fake_redis, monkeypatch):
    from app.tasks import notification_tasks

    reg = await _register(client, email="user@test.com", password="pass1234", full_name="User")
    user_id = reg.json()["data"]["id"]
    original = _stored_code(fake_redis, user_id)

    # Capture the email task and clear the registration cooldown so the login
    # attempt is allowed to issue a fresh code.
    calls = []
    monkeypatch.setattr(
        notification_tasks.send_verification_email, "delay",
        lambda *a, **kw: calls.append(a),
    )
    fake_redis.store.pop(f"{VERIFY_COOLDOWN_PREFIX}{user_id}", None)

    resp = await client.post("/api/v1/auth/login", json={
        "email": "user@test.com", "password": "pass1234"
    })
    assert resp.status_code == 403
    # A fresh code was issued and emailed.
    assert len(calls) == 1
    assert calls[0][0] == user_id
    new_code = _stored_code(fake_redis, user_id)
    assert calls[0][1] == new_code


@pytest.mark.asyncio
async def test_login_unverified_respects_cooldown(client, fake_redis, monkeypatch):
    from app.tasks import notification_tasks

    await _register(client, email="user@test.com", password="pass1234", full_name="User")
    calls = []
    monkeypatch.setattr(
        notification_tasks.send_verification_email, "delay",
        lambda *a, **kw: calls.append(a),
    )
    # Registration cooldown is still active — login must not send another code.
    resp = await client.post("/api/v1/auth/login", json={
        "email": "user@test.com", "password": "pass1234"
    })
    assert resp.status_code == 403
    assert calls == []


@pytest.mark.asyncio
async def test_resend_verification_unknown_email_is_generic(client):
    # No account — still a generic 200, no enumeration.
    resp = await client.post("/api/v1/auth/resend-verification", json={"email": "nobody@test.com"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_login_wrong_password(client, fake_redis):
    reg = await _register(client, email="x@test.com", password="correct1", full_name="X")
    user_id = reg.json()["data"]["id"]
    code = _stored_code(fake_redis, user_id)
    await client.post("/api/v1/auth/verify-email", json={"email": "x@test.com", "code": code})
    resp = await client.post("/api/v1/auth/login", json={"email": "x@test.com", "password": "wrong"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_auth(client):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_verify_email_notifies_admin_inbox(client, fake_redis, monkeypatch):
    from app.tasks import notification_tasks

    reg = await _register(client, email="user@test.com", password="pass1234", full_name="User")
    user_id = reg.json()["data"]["id"]
    code = _stored_code(fake_redis, user_id)

    calls = []
    monkeypatch.setattr(
        notification_tasks.send_new_user_admin_notification, "delay",
        lambda *a, **kw: calls.append(a),
    )

    verify = await client.post("/api/v1/auth/verify-email", json={
        "email": "user@test.com", "code": code,
    })
    assert verify.status_code == 200
    assert calls == [(user_id,)]


@pytest.mark.asyncio
async def test_failed_verification_does_not_notify_admin_inbox(client, monkeypatch):
    from app.tasks import notification_tasks

    await _register(client, email="user@test.com", password="pass1234", full_name="User")

    calls = []
    monkeypatch.setattr(
        notification_tasks.send_new_user_admin_notification, "delay",
        lambda *a, **kw: calls.append(a),
    )

    resp = await client.post("/api/v1/auth/verify-email", json={
        "email": "user@test.com", "code": "000000",
    })
    assert resp.status_code == 400
    assert calls == []


async def _verified_login(client, fake_redis, email="gone@test.com", password="pass1234",
                          full_name="Leaving User"):
    """Register, verify and log in, returning (user_id, access_token)."""
    reg = await _register(client, email=email, password=password, full_name=full_name)
    user_id = reg.json()["data"]["id"]
    code = _stored_code(fake_redis, user_id)
    verify = await client.post("/api/v1/auth/verify-email", json={"email": email, "code": code})
    return user_id, verify.json()["data"]["access_token"]


@pytest.mark.asyncio
async def test_deleting_an_account_notifies_the_admin_inbox(
    client, db_session, fake_redis, monkeypatch
):
    """Deletion is immediate, so the team inbox hears about it in the same request."""
    from app.tasks import notification_tasks

    user_id, token = await _verified_login(client, fake_redis)

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
            "DELETE", "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"}, json={},
        )

    assert resp.status_code == 200
    assert len(calls) == 1
    sent_user_id, full_name, email, provider, joined_at, actor = calls[0]
    assert sent_user_id == user_id
    assert full_name == "Leaving User"
    assert email == "gone@test.com"
    assert provider == "email"
    assert joined_at is not None
    assert actor == "user"


def test_account_deleted_admin_task_needs_no_database(monkeypatch):
    """The task must run purely from its arguments — the user row is already gone."""
    from app.tasks import notification_tasks

    sent = []

    async def _capture(**kw):
        sent.append(kw)

    monkeypatch.setattr("app.services.email_service.send_email", _capture)

    notification_tasks.send_account_deleted_admin_notification(
        "0f8fad5b-d9cb-469f-a165-70867728950e",
        "Ghost User",
        "ghost@test.com",
        "google",
        "2026-01-15T09:00:00+00:00",
    )

    assert len(sent) == 1
    assert sent[0]["to"] == "kida.audio@gmail.com"
    assert "ghost@test.com" in sent[0]["subject"]
    assert "ghost@test.com" in sent[0]["text"]
    assert "Ghost User" in sent[0]["text"]
    assert "google" in sent[0]["text"]
    assert "Jan 15, 2026" in sent[0]["text"]


def test_account_deleted_admin_task_skipped_without_recipient(monkeypatch):
    from app.config import get_settings
    from app.tasks import notification_tasks

    sent = []

    async def _capture(**kw):
        sent.append(kw)

    monkeypatch.setattr("app.services.email_service.send_email", _capture)
    get_settings.cache_clear()
    monkeypatch.setenv("ADMIN_NOTIFICATION_EMAIL", "")
    try:
        notification_tasks.send_account_deleted_admin_notification(
            "abc", "Ghost", "ghost@test.com", "email", None,
        )
        assert sent == []
    finally:
        get_settings.cache_clear()


# --- Google sign-in ---------------------------------------------------------

def _google_client(response=None, error=None):
    """A stand-in httpx client whose GET returns `response` or raises `error`."""
    c = AsyncMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.get = AsyncMock(return_value=response, side_effect=error)
    return c


def _userinfo(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = str(payload)
    resp.json.return_value = payload
    return resp


async def _google_login(client, **patch_kwargs):
    with patch(
        "app.services.oauth_service.httpx.AsyncClient",
        return_value=_google_client(**patch_kwargs),
    ):
        return await client.post(
            "/api/v1/auth/oauth/google/token", json={"access_token": "ya29.token"}
        )


@pytest.mark.asyncio
async def test_google_login_success(client):
    resp = await _google_login(client, response=_userinfo({
        "sub": "google-uid-1", "email": "gio@test.com", "name": "Gio",
    }))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["data"]["access_token"]
    assert body["data"]["full_name"] == "Gio"


@pytest.mark.asyncio
async def test_google_login_missing_access_token_is_422(client):
    resp = await client.post("/api/v1/auth/oauth/google/token", json={})
    assert resp.status_code == 422
    assert "access_token" in resp.json()["message"]


@pytest.mark.asyncio
async def test_google_login_rejected_token_is_401(client):
    payload = {"error": {"code": 401, "message": "Invalid Credentials"}}
    resp = await _google_login(client, response=_userinfo(payload, status_code=401))
    assert resp.status_code == 401
    body = resp.json()
    assert body["status"] == "error"
    assert "Invalid Credentials" in body["message"]


@pytest.mark.asyncio
async def test_google_login_without_email_scope_is_422_not_500(client):
    """The profile authenticates but carries no address — the old code raised
    KeyError here and the client saw "An unexpected error occurred"."""
    resp = await _google_login(client, response=_userinfo({"sub": "google-uid-2"}))
    assert resp.status_code == 422
    assert "email address" in resp.json()["message"]


@pytest.mark.asyncio
async def test_google_login_unreachable_google_is_503_not_500(client):
    resp = await _google_login(client, error=httpx.ConnectTimeout("timed out"))
    assert resp.status_code == 503
    assert "Could not reach Google" in resp.json()["message"]


@pytest.mark.asyncio
async def test_google_login_google_outage_is_502_not_500(client):
    resp = await _google_login(client, response=_userinfo({}, status_code=500))
    assert resp.status_code == 502
    assert "Google could not complete" in resp.json()["message"]


@pytest.mark.asyncio
async def test_google_login_suspended_account_is_401(client, db_session):
    from sqlalchemy import select
    from app.models.user import User

    await _google_login(client, response=_userinfo({
        "sub": "google-uid-3", "email": "banned@test.com", "name": "Banned",
    }))
    user = await db_session.scalar(select(User).where(User.email == "banned@test.com"))
    user.is_suspended = True
    user.suspension_reason = "Spam"
    await db_session.commit()

    resp = await _google_login(client, response=_userinfo({
        "sub": "google-uid-3", "email": "banned@test.com", "name": "Banned",
    }))
    assert resp.status_code == 401
    assert resp.json()["message"] == "Account suspended: Spam"


@pytest.mark.asyncio
async def test_google_login_null_name_falls_back_to_email_local_part(client):
    """`{"name": null}` used to reach the NOT NULL constraint on full_name."""
    resp = await _google_login(client, response=_userinfo({
        "sub": "google-uid-4", "email": "nameless@test.com", "name": None,
    }))
    assert resp.status_code == 200
    assert resp.json()["data"]["full_name"] == "nameless"


@pytest.mark.asyncio
async def test_google_login_overlong_avatar_is_dropped_not_fatal(client):
    """An avatar is cosmetic — too long for the column means drop it, not 500."""
    resp = await _google_login(client, response=_userinfo({
        "sub": "google-uid-5", "email": "big@test.com", "name": "Big",
        "picture": "https://lh3.googleusercontent.com/" + "x" * 600,
    }))
    assert resp.status_code == 200
    assert resp.json()["data"]["avatar_url"] is None


@pytest.mark.asyncio
async def test_google_login_overlong_name_is_truncated(client):
    resp = await _google_login(client, response=_userinfo({
        "sub": "google-uid-6", "email": "long@test.com", "name": "N" * 400,
    }))
    assert resp.status_code == 200
    assert len(resp.json()["data"]["full_name"]) == 255


@pytest.mark.asyncio
async def test_google_login_overlong_email_is_422(client):
    resp = await _google_login(client, response=_userinfo({
        "sub": "google-uid-7", "email": "e" * 300 + "@test.com", "name": "E",
    }))
    assert resp.status_code == 422
    assert "too long to register" in resp.json()["message"]


@pytest.mark.asyncio
async def test_google_login_non_string_email_is_502(client):
    """A payload that is not shaped like Google's docs would otherwise reach
    asyncpg as a raw DataError."""
    resp = await _google_login(client, response=_userinfo({
        "sub": "google-uid-8", "email": {"weird": 1}, "name": "W",
    }))
    assert resp.status_code == 502
