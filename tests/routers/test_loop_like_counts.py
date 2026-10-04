"""Every endpoint that returns a loop says how many likes it has."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.download import Download
from app.models.like import Like
from tests.routers.test_admin_loop_listing import _admin, _headers, _like, _loop


@pytest.fixture
async def liked(db_session):
    """An admin, a loop with 3 likes (one of them the admin's) and an unliked one."""
    admin = await _admin(db_session)
    popular = await _loop(db_session, admin.id, "Popular", "ready")
    unliked = await _loop(db_session, admin.id, "Unliked", "ready")
    await _like(db_session, popular, 2)
    db_session.add(Like(id=uuid.uuid4(), user_id=admin.id, loop_id=popular.id))
    await db_session.commit()
    return admin, popular, unliked


def _counts(resp):
    assert resp.status_code == 200, resp.text
    return {i["title"]: i["like_count"] for i in resp.json()["data"]["items"]}


@pytest.mark.asyncio
async def test_public_listing(client, liked):
    admin, _, _ = liked
    resp = await client.get("/api/v1/loops", headers=_headers(admin))
    assert _counts(resp) == {"Popular": 3, "Unliked": 0}


@pytest.mark.asyncio
async def test_public_listing_sorts_most_liked(client, liked):
    admin, _, _ = liked
    resp = await client.get("/api/v1/loops?sort=most_liked", headers=_headers(admin))
    assert list(_counts(resp)) == ["Popular", "Unliked"]


@pytest.mark.asyncio
async def test_detail(client, liked):
    admin, popular, unliked = liked
    for loop, expected in ((popular, 3), (unliked, 0)):
        resp = await client.get(f"/api/v1/loops/{loop.id}", headers=_headers(admin))
        assert resp.status_code == 200
        assert resp.json()["data"]["like_count"] == expected


@pytest.mark.asyncio
async def test_liked_loops(client, liked):
    admin, _, _ = liked
    resp = await client.get("/api/v1/likes/loops", headers=_headers(admin))
    assert _counts(resp) == {"Popular": 3}


@pytest.mark.asyncio
async def test_download_history(client, db_session, liked):
    admin, popular, unliked = liked
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    for loop in (popular, unliked):
        db_session.add(Download(
            id=uuid.uuid4(), user_id=admin.id, loop_id=loop.id,
            download_url="https://example.test/x", expires_at=expires,
        ))
    await db_session.commit()

    resp = await client.get("/api/v1/downloads", headers=_headers(admin))
    assert _counts(resp) == {"Popular": 3, "Unliked": 0}


@pytest.mark.asyncio
async def test_like_and_unlike_return_the_new_count(client, liked):
    admin, _, unliked = liked
    url = f"/api/v1/loops/{unliked.id}/like"

    resp = await client.post(url, headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["data"] == {"like_count": 1}

    resp = await client.delete(url, headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["data"] == {"like_count": 0}


@pytest.mark.asyncio
async def test_update_response(client, liked):
    admin, popular, _ = liked
    resp = await client.put(
        f"/api/v1/admin/loops/{popular.id}",
        data={"title": "Renamed"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["like_count"] == 3
