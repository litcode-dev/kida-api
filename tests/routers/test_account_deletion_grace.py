"""The 30-day deletion grace period: soft delete, blocked access, restore, purge."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.models.user import User, UserRole
from app.services import auth_service
from app.services.auth_service import (
    VERIFY_COOLDOWN_PREFIX,
    VERIFY_PREFIX,
    create_access_token,
    hash_password,
)

PASSWORD = "pass1234"


async def _make_user(db, email="leaver@test.com", verified=True):
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


async def _delete_account(client, user):
    return await client.request(
        "DELETE", "/api/v1/auth/me", headers=_headers(user), json={},
    )


async def _login(client, email="leaver@test.com", password=PASSWORD):
    return await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )


async def _age_deletion(db, user, days):
    """Backdate the pending deletion so the grace window has passed."""
    user.deleted_at = datetime.now(timezone.utc) - timedelta(days=days)
    await db.commit()
    await db.refresh(user)


# ── Soft delete ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_account_keeps_the_row_and_stamps_deleted_at(client, db_session):
    user = await _make_user(db_session)

    resp = await _delete_account(client, user)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["grace_days"] == 30
    assert data["restorable"] is True

    await db_session.refresh(user)
    assert user.deleted_at is not None
    # The row is still there — nothing has been erased yet.
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == user.id)
    ) == 1


@pytest.mark.asyncio
async def test_delete_account_reports_the_purge_date_30_days_out(client, db_session):
    user = await _make_user(db_session)

    resp = await _delete_account(client, user)

    due = datetime.fromisoformat(resp.json()["data"]["purge_due_at"])
    expected = datetime.now(timezone.utc) + timedelta(days=30)
    assert abs((due - expected).total_seconds()) < 120


@pytest.mark.asyncio
async def test_pending_deletion_account_cannot_use_its_access_token(client, db_session):
    """The token issued before deletion stays cryptographically valid — the
    middleware has to reject it."""
    user = await _make_user(db_session)
    headers = _headers(user)

    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    await _delete_account(client, user)

    resp = await client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_pending_deletion_account_cannot_refresh_its_session(client, db_session):
    user = await _make_user(db_session)
    login = await _login(client)
    refresh_token = login.json()["data"]["refresh_token"]

    await _delete_account(client, user)

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 401


# ── Restore by logging back in ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_within_grace_restores_the_account(client, db_session):
    user = await _make_user(db_session)
    await _delete_account(client, user)

    resp = await _login(client)

    assert resp.status_code == 200
    assert "access_token" in resp.json()["data"]
    await db_session.refresh(user)
    assert user.deleted_at is None


@pytest.mark.asyncio
async def test_restored_account_works_normally_again(client, db_session):
    user = await _make_user(db_session)
    await _delete_account(client, user)

    login = await _login(client)
    token = login.json()["data"]["access_token"]

    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted_at"] is None


@pytest.mark.asyncio
async def test_login_after_grace_expires_is_refused(client, db_session):
    """Past the window the account is owed deletion, even if the purge job is late."""
    user = await _make_user(db_session)
    await _delete_account(client, user)
    await _age_deletion(db_session, user, days=31)

    resp = await _login(client)

    assert resp.status_code == 401
    await db_session.refresh(user)
    assert user.deleted_at is not None  # not resurrected


@pytest.mark.asyncio
async def test_wrong_password_does_not_restore(client, db_session):
    user = await _make_user(db_session)
    await _delete_account(client, user)

    resp = await _login(client, password="wrong-password")

    assert resp.status_code == 401
    await db_session.refresh(user)
    assert user.deleted_at is not None


@pytest.mark.asyncio
async def test_suspended_account_is_not_restored_by_logging_in(client, db_session):
    user = await _make_user(db_session)
    await _delete_account(client, user)
    user.is_suspended = True
    await db_session.commit()

    resp = await _login(client)

    assert resp.status_code == 401
    await db_session.refresh(user)
    assert user.deleted_at is not None


# ── Restore by registering again ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_registering_again_within_grace_is_not_a_duplicate(client, db_session):
    user = await _make_user(db_session)
    await _delete_account(client, user)

    resp = await client.post("/api/v1/auth/register", json={
        "email": "leaver@test.com", "password": "brand-new-pass", "full_name": "Back Again",
    })

    assert resp.status_code == 200
    # Same account, not a second row.
    assert resp.json()["data"]["id"] == str(user.id)
    assert await db_session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.asyncio
async def test_re_registering_restores_only_after_the_code_is_verified(
    client, db_session, fake_redis
):
    """Ownership of the inbox has to be proven before the deletion is taken back."""
    user = await _make_user(db_session)
    await _delete_account(client, user)

    await client.post("/api/v1/auth/register", json={
        "email": "leaver@test.com", "password": "brand-new-pass", "full_name": "Back Again",
    })

    # Still pending deletion at this point.
    await db_session.refresh(user)
    assert user.deleted_at is not None

    code = fake_redis.store[f"{VERIFY_PREFIX}{user.id}"]
    resp = await client.post("/api/v1/auth/verify-email", json={
        "email": "leaver@test.com", "code": code,
    })

    assert resp.status_code == 200
    await db_session.refresh(user)
    assert user.deleted_at is None
    assert user.is_verified is True
    assert user.full_name == "Back Again"


@pytest.mark.asyncio
async def test_re_registered_user_can_log_in_with_the_new_password(
    client, db_session, fake_redis
):
    user = await _make_user(db_session)
    await _delete_account(client, user)
    await client.post("/api/v1/auth/register", json={
        "email": "leaver@test.com", "password": "brand-new-pass", "full_name": "Back Again",
    })
    code = fake_redis.store[f"{VERIFY_PREFIX}{user.id}"]
    await client.post("/api/v1/auth/verify-email", json={
        "email": "leaver@test.com", "code": code,
    })
    fake_redis.store.pop(f"{VERIFY_COOLDOWN_PREFIX}{user.id}", None)

    resp = await _login(client, password="brand-new-pass")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_registering_over_a_live_account_is_still_a_conflict(client, db_session):
    await _make_user(db_session)

    resp = await client.post("/api/v1/auth/register", json={
        "email": "leaver@test.com", "password": "another-pass", "full_name": "Impostor",
    })

    assert resp.status_code == 409


# ── OAuth return ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_oauth_sign_in_within_grace_restores_the_account(db_session):
    user = await _make_user(db_session, email="oauth@test.com")
    user.oauth_provider = "google"
    user.oauth_provider_id = "google-123"
    user.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    with patch("app.services.email_service.send_email", new=AsyncMock()):
        found = await auth_service.find_or_create_oauth_user(
            db_session, email="oauth@test.com", full_name="OAuth User",
            provider="google", provider_id="google-123",
        )

    assert found.id == user.id
    assert found.deleted_at is None


@pytest.mark.asyncio
async def test_oauth_sign_in_after_grace_is_refused(db_session):
    from app.exceptions import UnauthorizedError

    user = await _make_user(db_session, email="oauth@test.com")
    user.oauth_provider = "google"
    user.oauth_provider_id = "google-123"
    await db_session.commit()
    await _age_deletion(db_session, user, days=31)

    with pytest.raises(UnauthorizedError):
        await auth_service.find_or_create_oauth_user(
            db_session, email="oauth@test.com", full_name="OAuth User",
            provider="google", provider_id="google-123",
        )


# ── Purge ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_removes_only_accounts_past_the_window(client, db_session, fake_redis):
    expired = await _make_user(db_session, email="expired@test.com")
    recent = await _make_user(db_session, email="recent@test.com")
    live = await _make_user(db_session, email="live@test.com")

    await _delete_account(client, expired)
    await _delete_account(client, recent)
    await _age_deletion(db_session, expired, days=31)

    purged = await auth_service.purge_expired_accounts(db_session, fake_redis)

    assert purged == [str(expired.id)]
    remaining = set(await db_session.scalars(select(User.email)))
    assert remaining == {"recent@test.com", "live@test.com"}
    assert live.deleted_at is None


@pytest.mark.asyncio
async def test_purge_is_a_no_op_when_nothing_is_due(client, db_session, fake_redis):
    user = await _make_user(db_session)
    await _delete_account(client, user)

    assert await auth_service.purge_expired_accounts(db_session, fake_redis) == []
    assert await db_session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.asyncio
async def test_purged_account_frees_its_email_for_a_fresh_signup(
    client, db_session, fake_redis
):
    user = await _make_user(db_session)
    await _delete_account(client, user)
    await _age_deletion(db_session, user, days=31)
    await auth_service.purge_expired_accounts(db_session, fake_redis)

    resp = await client.post("/api/v1/auth/register", json={
        "email": "leaver@test.com", "password": PASSWORD, "full_name": "Someone New",
    })

    assert resp.status_code == 200
    assert resp.json()["data"]["id"] != str(user.id)


# ── Mail suppression ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_deletion_accounts_are_excluded_from_broadcasts(client, db_session):
    from app.schemas.broadcast import BroadcastAudience
    from app.services import broadcast_service

    staying = await _make_user(db_session, email="staying@test.com")
    leaving = await _make_user(db_session, email="leaver@test.com")
    await _delete_account(client, leaving)

    emails = await broadcast_service.resolve_recipients(db_session, BroadcastAudience.all)

    assert emails == ["staying@test.com"]
    assert staying.deleted_at is None


# ── Grace helpers ────────────────────────────────────────────────────────────

def test_within_deletion_grace_boundaries():
    now = datetime.now(timezone.utc)
    assert auth_service.within_deletion_grace(None) is False
    assert auth_service.within_deletion_grace(now) is True
    assert auth_service.within_deletion_grace(now - timedelta(days=29)) is True
    assert auth_service.within_deletion_grace(now - timedelta(days=31)) is False


def test_purge_due_at_handles_a_naive_timestamp():
    """Postgres can hand back a naive datetime depending on the driver."""
    naive = datetime(2026, 8, 9, 12, 0)
    assert auth_service.purge_due_at(naive) == datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


# ── The welcome-back email ───────────────────────────────────────────────────
#
# Every way of coming back inside the window has to tell the user, both because
# it confirms the deletion is off and because it is the signal that something
# is wrong if they did not do it.

@pytest.mark.asyncio
async def test_login_restore_emails_the_user(client, db_session):
    user = await _make_user(db_session)
    await _delete_account(client, user)

    sent = []

    async def _capture(**kw):
        sent.append(kw)

    with patch("app.services.email_service.send_email", new=_capture):
        resp = await _login(client)

    assert resp.status_code == 200
    assert [s["to"] for s in sent] == ["leaver@test.com"]
    assert sent[0]["subject"] == "Your Kida account is back"
    assert "Leaver" in sent[0]["text"]
    assert "cancelled" in sent[0]["text"]
    # It doubles as a break-in warning.
    assert "change your password" in sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_oauth_restore_emails_the_user(db_session):
    user = await _make_user(db_session, email="oauth@test.com")
    user.oauth_provider = "google"
    user.oauth_provider_id = "google-123"
    user.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    sent = []

    async def _capture(**kw):
        sent.append(kw)

    with patch("app.services.email_service.send_email", new=_capture):
        await auth_service.find_or_create_oauth_user(
            db_session, email="oauth@test.com", full_name="OAuth User",
            provider="google", provider_id="google-123",
        )

    assert [s["subject"] for s in sent] == ["Your Kida account is back"]


@pytest.mark.asyncio
async def test_re_registration_restore_emails_the_user(client, db_session, fake_redis):
    user = await _make_user(db_session)
    await _delete_account(client, user)
    await client.post("/api/v1/auth/register", json={
        "email": "leaver@test.com", "password": "brand-new-pass", "full_name": "Back Again",
    })
    code = fake_redis.store[f"{VERIFY_PREFIX}{user.id}"]

    sent = []

    async def _capture(**kw):
        sent.append(kw)

    with patch("app.services.email_service.send_email", new=_capture):
        resp = await client.post("/api/v1/auth/verify-email", json={
            "email": "leaver@test.com", "code": code,
        })

    assert resp.status_code == 200
    assert [s["subject"] for s in sent] == ["Your Kida account is back"]


@pytest.mark.asyncio
async def test_returning_user_is_not_treated_as_a_new_signup(
    client, db_session, fake_redis, monkeypatch
):
    """No "Welcome to Kida" and no new-signup notification — the account existed."""
    from app.tasks import notification_tasks

    user = await _make_user(db_session)
    await _delete_account(client, user)
    await client.post("/api/v1/auth/register", json={
        "email": "leaver@test.com", "password": "brand-new-pass", "full_name": "Back Again",
    })
    code = fake_redis.store[f"{VERIFY_PREFIX}{user.id}"]

    queued = []
    monkeypatch.setattr(
        notification_tasks.send_registration_email, "delay",
        lambda *a, **kw: queued.append("welcome"),
    )
    monkeypatch.setattr(
        notification_tasks.send_new_user_admin_notification, "delay",
        lambda *a, **kw: queued.append("new_signup_admin"),
    )

    with patch("app.services.email_service.send_email", new=AsyncMock()):
        await client.post("/api/v1/auth/verify-email", json={
            "email": "leaver@test.com", "code": code,
        })

    assert queued == []


@pytest.mark.asyncio
async def test_a_genuinely_new_signup_still_gets_the_welcome(
    client, fake_redis, monkeypatch
):
    """The returning-user branch must not swallow the normal signup path."""
    from app.tasks import notification_tasks

    reg = await client.post("/api/v1/auth/register", json={
        "email": "brand-new@test.com", "password": "pass1234", "full_name": "New Person",
    })
    user_id = reg.json()["data"]["id"]
    code = fake_redis.store[f"{VERIFY_PREFIX}{user_id}"]

    queued = []
    monkeypatch.setattr(
        notification_tasks.send_registration_email, "delay",
        lambda *a, **kw: queued.append("welcome"),
    )
    monkeypatch.setattr(
        notification_tasks.send_new_user_admin_notification, "delay",
        lambda *a, **kw: queued.append("new_signup_admin"),
    )

    await client.post("/api/v1/auth/verify-email", json={
        "email": "brand-new@test.com", "code": code,
    })

    assert queued == ["welcome", "new_signup_admin"]


@pytest.mark.asyncio
async def test_a_failed_return_sends_nothing(client, db_session):
    """Wrong password, or past the window — no mail either way."""
    user = await _make_user(db_session)
    await _delete_account(client, user)

    sent = []

    async def _capture(**kw):
        sent.append(kw)

    with patch("app.services.email_service.send_email", new=_capture):
        assert (await _login(client, password="wrong-password")).status_code == 401
        await _age_deletion(db_session, user, days=31)
        assert (await _login(client)).status_code == 401

    assert sent == []
