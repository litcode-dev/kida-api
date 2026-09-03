"""An admin has to be able to see a loop that is not browsable.

The public listing shows `ready` loops only, and it was the only listing that
returned loops to an administrator — so replacing a loop's audio, which puts it
back into `processing`, made it vanish from the dashboard as though the update
had deleted it.
"""
import uuid
from decimal import Decimal

import pytest

from app.models.loop import Loop, Genre, TempoFeel
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


async def _admin(db):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Admin",
        role=UserRole.admin,
    )
    db.add(user)
    await db.commit()
    return user


async def _loop(db, user_id, title, status):
    loop = Loop(
        id=uuid.uuid4(),
        title=title,
        slug=f"{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        bpm=90,
        duration=4,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("0.00"),
        is_free=True,
        is_paid=False,
        status=status,
        created_by=user_id,
    )
    db.add(loop)
    await db.commit()
    return loop


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


@pytest.mark.asyncio
async def test_a_reprocessing_loop_is_listed(client, db_session):
    admin = await _admin(db_session)
    await _loop(db_session, admin.id, "Ready Loop", "ready")
    await _loop(db_session, admin.id, "Reprocessing Loop", "processing")
    await _loop(db_session, admin.id, "Broken Loop", "failed")

    resp = await client.get("/api/v1/admin/loops", headers=_headers(admin))

    assert resp.status_code == 200
    titles = {item["title"] for item in resp.json()["data"]["items"]}
    assert titles == {"Ready Loop", "Reprocessing Loop", "Broken Loop"}
    assert resp.json()["data"]["total"] == 3


@pytest.mark.asyncio
async def test_the_public_listing_still_hides_it(client, db_session):
    """A loop with no playable audio does not belong in the catalogue."""
    admin = await _admin(db_session)
    await _loop(db_session, admin.id, "Ready Loop", "ready")
    await _loop(db_session, admin.id, "Reprocessing Loop", "processing")

    resp = await client.get("/api/v1/loops", headers=_headers(admin))

    titles = {item["title"] for item in resp.json()["data"]["items"]}
    assert titles == {"Ready Loop"}


@pytest.mark.asyncio
async def test_status_is_reported_for_each_loop(client, db_session):
    """Without it the listing cannot tell a loop that is briefly reprocessing
    from one whose job failed hours ago."""
    admin = await _admin(db_session)
    await _loop(db_session, admin.id, "Broken Loop", "failed")

    resp = await client.get("/api/v1/admin/loops", headers=_headers(admin))

    assert resp.json()["data"]["items"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_the_listing_narrows_to_one_status(client, db_session):
    admin = await _admin(db_session)
    await _loop(db_session, admin.id, "Ready Loop", "ready")
    await _loop(db_session, admin.id, "Broken Loop", "failed")

    resp = await client.get("/api/v1/admin/loops?status=failed", headers=_headers(admin))

    items = resp.json()["data"]["items"]
    assert [item["title"] for item in items] == ["Broken Loop"]
    assert resp.json()["data"]["total"] == 1


@pytest.mark.asyncio
async def test_a_status_no_loop_can_hold_is_refused(client, db_session):
    """Silently returning nothing would read as an empty catalogue."""
    admin = await _admin(db_session)

    resp = await client.get("/api/v1/admin/loops?status=broken", headers=_headers(admin))

    assert resp.status_code == 422
    assert "ready, processing, failed" in resp.json()["message"]


@pytest.mark.asyncio
async def test_other_filters_still_apply(client, db_session):
    admin = await _admin(db_session)
    await _loop(db_session, admin.id, "Findable Loop", "processing")
    await _loop(db_session, admin.id, "Other Loop", "processing")

    resp = await client.get("/api/v1/admin/loops?search=Findable", headers=_headers(admin))

    assert [i["title"] for i in resp.json()["data"]["items"]] == ["Findable Loop"]


@pytest.mark.asyncio
async def test_the_listing_is_admin_only(client, db_session):
    admin = await _admin(db_session)
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="User",
        role=UserRole.user,
    )
    db_session.add(user)
    await db_session.commit()
    await _loop(db_session, admin.id, "Reprocessing Loop", "processing")

    resp = await client.get("/api/v1/admin/loops", headers=_headers(user))

    assert resp.status_code == 403
