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
