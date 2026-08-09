import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.drone_pad import Drone, DronePad, DronePadCategory, MusicalKey
from app.models.download_grant import DownloadGrant, DownloadGrantType
from app.models.iap_subscription import (
    IapSubscription,
    IapSubscriptionSource,
    IapSubscriptionStatus,
)
from app.models.like import Like
from app.models.loop import Genre, Loop, TempoFeel
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


async def _make_user(db, role=UserRole.user):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Test User",
        role=role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _delete(client, admin, user_id):
    return await client.request(
        "DELETE", f"/api/v1/admin/users/{user_id}", headers=_headers(admin)
    )


async def _add_loop(db, user_id):
    loop = Loop(
        id=uuid.uuid4(), title="L", slug=f"l-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat, bpm=100, duration=10, tempo_feel=TempoFeel.mid,
        tags=[], price=Decimal("1.00"), is_free=True, is_paid=False,
        created_by=user_id,
    )
    db.add(loop)
    await db.commit()
    return loop


async def _add_premium(db, user_id):
    db.add(IapSubscription(
        user_id=user_id,
        product_id="kida.premium.monthly",
        store_transaction_id=str(uuid.uuid4()),
        status=IapSubscriptionStatus.active,
        source=IapSubscriptionSource.store,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    ))
    await db.commit()


async def _count(db, model, **where):
    q = select(func.count()).select_from(model)
    for col, val in where.items():
        q = q.where(getattr(model, col) == val)
    return await db.scalar(q)


# ── Access control ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_user_requires_authentication(client, db_session):
    victim = await _make_user(db_session)
    resp = await client.request("DELETE", f"/api/v1/admin/users/{victim.id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_cannot_delete_a_user(client, db_session):
    caller = await _make_user(db_session, role=UserRole.producer)
    victim = await _make_user(db_session)
    resp = await _delete(client, caller, victim.id)
    assert resp.status_code == 403
    assert await _count(db_session, User, id=victim.id) == 1


@pytest.mark.asyncio
async def test_admin_cannot_delete_another_admin(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    other = await _make_user(db_session, role=UserRole.admin)
    resp = await _delete(client, admin, other.id)
    assert resp.status_code == 403
    assert await _count(db_session, User, id=other.id) == 1


@pytest.mark.asyncio
async def test_admin_cannot_delete_themselves(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    resp = await _delete(client, admin, admin.id)
    assert resp.status_code == 403
    assert await _count(db_session, User, id=admin.id) == 1


@pytest.mark.asyncio
async def test_delete_unknown_user_returns_404(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    resp = await _delete(client, admin, uuid.uuid4())
    assert resp.status_code == 404


# ── Happy path ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_deletes_a_user(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    victim = await _make_user(db_session)

    resp = await _delete(client, admin, victim.id)

    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert await _count(db_session, User, id=victim.id) == 0


@pytest.mark.asyncio
async def test_deleting_a_producer_removes_their_content(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    producer = await _make_user(db_session, role=UserRole.producer)
    loop = await _add_loop(db_session, producer.id)

    resp = await _delete(client, admin, producer.id)

    assert resp.status_code == 200
    assert await _count(db_session, Loop, id=loop.id) == 0


@pytest.mark.asyncio
async def test_deleting_a_user_removes_their_likes_and_grants(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    victim = await _make_user(db_session)
    other = await _make_user(db_session, role=UserRole.producer)
    loop = await _add_loop(db_session, other.id)

    db_session.add(Like(id=uuid.uuid4(), user_id=victim.id, loop_id=loop.id))
    db_session.add(DownloadGrant(
        user_id=victim.id, item_type=DownloadGrantType.loop, item_id=str(loop.id),
    ))
    await db_session.commit()

    resp = await _delete(client, admin, victim.id)

    assert resp.status_code == 200
    assert await _count(db_session, Like, user_id=victim.id) == 0
    assert await _count(db_session, DownloadGrant, user_id=victim.id) == 0
    # Another producer's loop is untouched.
    assert await _count(db_session, Loop, id=loop.id) == 1


# ── Foreign keys that used to block the delete ───────────────────────────────

@pytest.mark.asyncio
async def test_deleting_a_premium_subscriber_succeeds(client, db_session):
    """iap_subscriptions has no ON DELETE CASCADE and was not cleaned up."""
    admin = await _make_user(db_session, role=UserRole.admin)
    victim = await _make_user(db_session)
    await _add_premium(db_session, victim.id)

    resp = await _delete(client, admin, victim.id)

    assert resp.status_code == 200
    assert await _count(db_session, User, id=victim.id) == 0
    assert await _count(db_session, IapSubscription, user_id=victim.id) == 0


@pytest.mark.asyncio
async def test_deleting_a_user_with_a_drone_in_another_category_succeeds(client, db_session):
    """Drones carry their own created_by and need not sit under the user's category."""
    admin = await _make_user(db_session, role=UserRole.admin)
    victim = await _make_user(db_session, role=UserRole.producer)
    other = await _make_user(db_session, role=UserRole.producer)

    category = DronePadCategory(
        id=uuid.uuid4(), name=f"cat-{uuid.uuid4().hex[:8]}", created_by=other.id
    )
    db_session.add(category)
    await db_session.flush()
    drone = Drone(
        id=uuid.uuid4(), title="D", price=Decimal("0.00"), is_free=True,
        category_id=category.id, created_by=victim.id,
    )
    db_session.add(drone)
    await db_session.flush()
    db_session.add(DronePad(
        id=uuid.uuid4(), drone_id=drone.id, key=MusicalKey.C, duration=30, status="ready",
    ))
    await db_session.commit()

    resp = await _delete(client, admin, victim.id)

    assert resp.status_code == 200
    assert await _count(db_session, User, id=victim.id) == 0
    assert await _count(db_session, Drone, id=drone.id) == 0
    # The other producer's category survives.
    assert await _count(db_session, DronePadCategory, id=category.id) == 1


# ── Notifications ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_delete_notifies_the_team_inbox_as_admin_initiated(
    client, db_session, monkeypatch
):
    from app.tasks import notification_tasks

    admin = await _make_user(db_session, role=UserRole.admin)
    victim = await _make_user(db_session)

    calls = []
    monkeypatch.setattr(
        notification_tasks.send_account_deleted_admin_notification, "delay",
        lambda *a, **kw: calls.append(a),
    )

    resp = await _delete(client, admin, victim.id)

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == str(victim.id)
    assert calls[0][-1] == "admin"


@pytest.mark.asyncio
async def test_self_delete_still_reports_the_user_as_actor(client, db_session, monkeypatch):
    from app.tasks import notification_tasks

    victim = await _make_user(db_session)

    calls = []
    monkeypatch.setattr(
        notification_tasks.send_account_deleted_admin_notification, "delay",
        lambda *a, **kw: calls.append(a),
    )

    resp = await client.request(
        "DELETE", "/api/v1/auth/me", headers=_headers(victim), json={},
    )

    assert resp.status_code == 200
    assert calls[0][-1] == "user"
