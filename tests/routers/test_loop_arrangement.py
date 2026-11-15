"""Admins arrange loops by hand, and listings follow with `sort=custom`.

Arranged loops lead in the order given; loops nobody has placed follow, newest
first, so a fresh upload is listed without anyone having to position it.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.loop import Loop, Genre, TempoFeel
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _user(db, role=UserRole.admin):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Someone",
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


async def _loops(db, owner, *titles):
    """One ready loop per title, each created a day after the one before."""
    loops = []
    for day, title in enumerate(titles):
        loop = Loop(
            id=uuid.uuid4(),
            title=title,
            slug=f"{title.lower()}-{uuid.uuid4().hex[:8]}",
            genre=Genre.afrobeat,
            bpm=90,
            duration=4,
            tempo_feel=TempoFeel.mid,
            tags=[],
            price=Decimal("0.00"),
            is_free=True,
            is_paid=False,
            status="ready",
            created_by=owner.id,
            created_at=_BASE_TIME + timedelta(days=day),
        )
        db.add(loop)
        loops.append(loop)
    await db.commit()
    return [str(loop.id) for loop in loops]


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _titles(client, user, path="/api/v1/loops", **params):
    resp = await client.get(path, params={"sort": "custom", **params}, headers=_auth(user))
    assert resp.status_code == 200, resp.text
    return [item["title"] for item in resp.json()["data"]["items"]]


async def _arrange(client, admin, ids):
    return await client.put(
        "/api/v1/admin/loops/order", json={"loop_ids": ids}, headers=_auth(admin)
    )


@pytest.mark.asyncio
async def test_custom_sort_is_newest_first_until_anything_is_arranged(client, db_session):
    admin = await _user(db_session)
    await _loops(db_session, admin, "A", "B", "C")

    assert await _titles(client, admin) == ["C", "B", "A"]


@pytest.mark.asyncio
async def test_arranged_loops_lead_and_the_rest_follow_newest_first(client, db_session):
    admin = await _user(db_session)
    a, b, c, d = await _loops(db_session, admin, "A", "B", "C", "D")

    resp = await _arrange(client, admin, [b, a])

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["loop_ids"] == [b, a]
    assert await _titles(client, admin) == ["B", "A", "D", "C"]


@pytest.mark.asyncio
async def test_a_partial_list_moves_those_loops_up_and_keeps_the_rest(client, db_session):
    admin = await _user(db_session)
    a, b, c, d = await _loops(db_session, admin, "A", "B", "C", "D")
    await _arrange(client, admin, [a, b, c])

    resp = await _arrange(client, admin, [c])

    assert resp.json()["data"]["loop_ids"] == [c, a, b]
    assert await _titles(client, admin) == ["C", "A", "B", "D"]


@pytest.mark.asyncio
async def test_a_loop_can_be_taken_out_of_the_arrangement(client, db_session):
    admin = await _user(db_session)
    a, b, c = await _loops(db_session, admin, "A", "B", "C")
    await _arrange(client, admin, [a, b])

    resp = await client.delete(f"/api/v1/admin/loops/{a}/position", headers=_auth(admin))

    assert resp.status_code == 200, resp.text
    assert await _titles(client, admin) == ["B", "C", "A"]


@pytest.mark.asyncio
async def test_custom_order_holds_within_filters_and_across_listings(client, db_session):
    admin = await _user(db_session)
    a, b, c, d = await _loops(db_session, admin, "A", "B", "C", "D")
    await _arrange(client, admin, [a, d, c, b])
    fast = await db_session.get(Loop, uuid.UUID(d))
    fast.bpm = 140
    await db_session.commit()

    assert await _titles(client, admin, bpm_max=100) == ["A", "C", "B"]
    assert await _titles(client, admin, path="/api/v1/admin/loops") == ["A", "D", "C", "B"]
    resp = await client.get(
        "/api/v1/admin/loops", params={"sort": "custom"}, headers=_auth(admin)
    )
    assert [item["position"] for item in resp.json()["data"]["items"]] == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_custom_order_pages_without_overlap(client, db_session):
    admin = await _user(db_session)
    ids = await _loops(db_session, admin, "A", "B", "C", "D", "E")
    await _arrange(client, admin, [ids[3], ids[1]])

    first = await _titles(client, admin, page=1, page_size=3)
    second = await _titles(client, admin, page=2, page_size=3)

    assert first + second == ["D", "B", "E", "C", "A"]


@pytest.mark.asyncio
async def test_other_sorts_ignore_the_arrangement(client, db_session):
    admin = await _user(db_session)
    a, b, c = await _loops(db_session, admin, "A", "B", "C")
    await _arrange(client, admin, [a, b, c])

    assert await _titles(client, admin, sort="newest") == ["C", "B", "A"]


@pytest.mark.asyncio
async def test_arranging_rejects_unknown_and_repeated_ids(client, db_session):
    admin = await _user(db_session)
    [a] = await _loops(db_session, admin, "A")

    unknown = await _arrange(client, admin, [a, str(uuid.uuid4())])
    repeated = await _arrange(client, admin, [a, a])
    empty = await _arrange(client, admin, [])

    assert unknown.status_code == 404
    assert repeated.status_code == 422
    assert empty.status_code == 422
    # A refused request changes nothing.
    resp = await client.get("/api/v1/admin/loops", headers=_auth(admin))
    assert resp.json()["data"]["items"][0]["position"] is None


@pytest.mark.asyncio
async def test_only_admins_can_arrange(client, db_session):
    producer = await _user(db_session, UserRole.producer)
    [a] = await _loops(db_session, producer, "A")

    arrange = await _arrange(client, producer, [a])
    unarrange = await client.delete(
        f"/api/v1/admin/loops/{a}/position", headers=_auth(producer)
    )

    assert arrange.status_code == 403
    assert unarrange.status_code == 403
