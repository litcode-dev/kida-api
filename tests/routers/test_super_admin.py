"""The super_admin role: reach, and the rank rules that guard admin accounts."""
import uuid

import pytest
from sqlalchemy import func, select

from app.models.user import ROLE_RANK, User, UserRole, outranks
from app.services.auth_service import create_access_token, hash_password


@pytest.fixture(autouse=True)
def _no_outbound(monkeypatch):
    """Suspend/delete send mail and sweep S3; nothing here asserts on either."""
    async def _noop_email(**kwargs):
        return None

    async def _noop_s3(key):
        return None

    monkeypatch.setattr("app.services.email_service.send_email", _noop_email)
    monkeypatch.setattr("app.services.s3_service.delete_object", _noop_s3)
    monkeypatch.setattr(
        "app.tasks.notification_tasks.send_account_deleted_admin_notification.delay",
        lambda *a, **kw: None,
    )


async def _make_user(db, role=UserRole.user):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("pass1234"),
        full_name="Test User",
        role=role,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _set_role(client, actor, target_id, role):
    return await client.put(
        f"/api/v1/admin/users/{target_id}/role?role={role.value}", headers=_headers(actor)
    )


async def _suspend(client, actor, target_id):
    return await client.put(
        f"/api/v1/admin/users/{target_id}/suspend",
        json={"reason": "testing"}, headers=_headers(actor),
    )


async def _delete(client, actor, target_id):
    return await client.request(
        "DELETE", f"/api/v1/admin/users/{target_id}", headers=_headers(actor)
    )


# ── Rank helper ──────────────────────────────────────────────────────────────

def test_super_admin_is_the_highest_role():
    assert ROLE_RANK[UserRole.super_admin] > ROLE_RANK[UserRole.admin]
    assert ROLE_RANK[UserRole.admin] > ROLE_RANK[UserRole.producer]
    assert ROLE_RANK[UserRole.producer] > ROLE_RANK[UserRole.user]


def test_a_role_never_outranks_itself():
    """This is what stops self-demotion and admin-on-admin actions."""
    for role in UserRole:
        assert outranks(role, role) is False


# ── Reach: a super admin can do everything an admin can ──────────────────────

@pytest.mark.asyncio
async def test_super_admin_reaches_admin_endpoints(client, db_session):
    caller = await _make_user(db_session, role=UserRole.super_admin)
    resp = await client.get("/api/v1/admin/users", headers=_headers(caller))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_super_admin_reaches_producer_endpoints(client, db_session):
    caller = await _make_user(db_session, role=UserRole.super_admin)
    resp = await client.get("/api/v1/producer/stem-packs", headers=_headers(caller))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_plain_user_still_cannot_reach_admin_endpoints(client, db_session):
    caller = await _make_user(db_session)
    resp = await client.get("/api/v1/admin/users", headers=_headers(caller))
    assert resp.status_code == 403


# ── Deleting ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_super_admin_can_delete_an_admin(client, db_session):
    caller = await _make_user(db_session, role=UserRole.super_admin)
    target = await _make_user(db_session, role=UserRole.admin)

    resp = await _delete(client, caller, target.id)

    assert resp.status_code == 200
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == target.id)
    ) == 0


@pytest.mark.asyncio
async def test_admin_still_cannot_delete_an_admin(client, db_session):
    caller = await _make_user(db_session, role=UserRole.admin)
    target = await _make_user(db_session, role=UserRole.admin)

    assert (await _delete(client, caller, target.id)).status_code == 403


@pytest.mark.asyncio
async def test_nobody_can_delete_a_super_admin(client, db_session):
    target = await _make_user(db_session, role=UserRole.super_admin)

    for role in (UserRole.admin, UserRole.super_admin):
        caller = await _make_user(db_session, role=role)
        assert (await _delete(client, caller, target.id)).status_code == 403

    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.id == target.id)
    ) == 1


@pytest.mark.asyncio
async def test_super_admin_cannot_delete_themselves(client, db_session):
    caller = await _make_user(db_session, role=UserRole.super_admin)
    assert (await _delete(client, caller, caller.id)).status_code == 403


# ── Suspending ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_super_admin_can_suspend_an_admin(client, db_session):
    caller = await _make_user(db_session, role=UserRole.super_admin)
    target = await _make_user(db_session, role=UserRole.admin)

    resp = await _suspend(client, caller, target.id)

    assert resp.status_code == 200
    await db_session.refresh(target)
    assert target.is_suspended is True


@pytest.mark.asyncio
async def test_admin_cannot_suspend_an_admin(client, db_session):
    caller = await _make_user(db_session, role=UserRole.admin)
    target = await _make_user(db_session, role=UserRole.admin)

    assert (await _suspend(client, caller, target.id)).status_code == 403
    await db_session.refresh(target)
    assert target.is_suspended is False


@pytest.mark.asyncio
async def test_super_admin_cannot_suspend_another_super_admin(client, db_session):
    caller = await _make_user(db_session, role=UserRole.super_admin)
    target = await _make_user(db_session, role=UserRole.super_admin)

    assert (await _suspend(client, caller, target.id)).status_code == 403


@pytest.mark.asyncio
async def test_admin_can_still_suspend_a_normal_user(client, db_session):
    caller = await _make_user(db_session, role=UserRole.admin)
    target = await _make_user(db_session)

    assert (await _suspend(client, caller, target.id)).status_code == 200


# ── Role changes ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_can_move_a_user_to_producer(client, db_session):
    caller = await _make_user(db_session, role=UserRole.admin)
    target = await _make_user(db_session)

    resp = await _set_role(client, caller, target.id, UserRole.producer)

    assert resp.status_code == 200
    await db_session.refresh(target)
    assert target.role == UserRole.producer


@pytest.mark.asyncio
async def test_admin_cannot_promote_anyone_to_admin(client, db_session):
    """Otherwise an admin mints peers at will."""
    caller = await _make_user(db_session, role=UserRole.admin)
    target = await _make_user(db_session)

    assert (await _set_role(client, caller, target.id, UserRole.admin)).status_code == 403
    await db_session.refresh(target)
    assert target.role == UserRole.user


@pytest.mark.asyncio
async def test_admin_cannot_promote_anyone_to_super_admin(client, db_session):
    """The privilege-escalation route: grant a role above yourself, then use it."""
    caller = await _make_user(db_session, role=UserRole.admin)
    target = await _make_user(db_session)

    assert (await _set_role(client, caller, target.id, UserRole.super_admin)).status_code == 403


@pytest.mark.asyncio
async def test_admin_cannot_demote_another_admin(client, db_session):
    caller = await _make_user(db_session, role=UserRole.admin)
    target = await _make_user(db_session, role=UserRole.admin)

    assert (await _set_role(client, caller, target.id, UserRole.user)).status_code == 403


@pytest.mark.asyncio
async def test_super_admin_can_promote_and_demote_admins(client, db_session):
    caller = await _make_user(db_session, role=UserRole.super_admin)
    target = await _make_user(db_session)

    assert (await _set_role(client, caller, target.id, UserRole.admin)).status_code == 200
    await db_session.refresh(target)
    assert target.role == UserRole.admin

    assert (await _set_role(client, caller, target.id, UserRole.user)).status_code == 200
    await db_session.refresh(target)
    assert target.role == UserRole.user


@pytest.mark.asyncio
async def test_super_admin_cannot_grant_super_admin(client, db_session):
    """Promotion to the top role is deliberately not self-service."""
    caller = await _make_user(db_session, role=UserRole.super_admin)
    target = await _make_user(db_session)

    assert (await _set_role(client, caller, target.id, UserRole.super_admin)).status_code == 403


@pytest.mark.asyncio
async def test_super_admin_cannot_change_another_super_admins_role(client, db_session):
    caller = await _make_user(db_session, role=UserRole.super_admin)
    target = await _make_user(db_session, role=UserRole.super_admin)

    assert (await _set_role(client, caller, target.id, UserRole.user)).status_code == 403


@pytest.mark.asyncio
async def test_nobody_can_change_their_own_role(client, db_session):
    for role in (UserRole.admin, UserRole.super_admin):
        caller = await _make_user(db_session, role=role)
        resp = await _set_role(client, caller, caller.id, UserRole.user)
        assert resp.status_code == 403
        await db_session.refresh(caller)
        assert caller.role == role


@pytest.mark.asyncio
async def test_role_change_on_unknown_user_is_404(client, db_session):
    caller = await _make_user(db_session, role=UserRole.super_admin)
    resp = await _set_role(client, caller, uuid.uuid4(), UserRole.producer)
    assert resp.status_code == 404
