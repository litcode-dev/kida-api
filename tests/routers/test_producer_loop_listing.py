"""A producer's own loop listing shows how many likes each loop has."""
import pytest

from app.models.user import UserRole
from tests.routers.test_admin_loop_listing import _admin, _headers, _like, _loop


async def _producer(db):
    user = await _admin(db)
    user.role = UserRole.producer
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_each_loop_carries_its_like_count(client, db_session):
    producer = await _producer(db_session)
    popular = await _loop(db_session, producer.id, "Popular", "ready")
    await _loop(db_session, producer.id, "Unliked", "ready")
    await _like(db_session, popular, 2)

    resp = await client.get("/api/v1/producer/loops", headers=_headers(producer))

    assert resp.status_code == 200
    counts = {i["title"]: i["like_count"] for i in resp.json()["data"]["items"]}
    assert counts == {"Popular": 2, "Unliked": 0}


@pytest.mark.asyncio
async def test_most_liked_sort_orders_by_like_count(client, db_session):
    producer = await _producer(db_session)
    one = await _loop(db_session, producer.id, "One", "ready")
    await _loop(db_session, producer.id, "Zero", "ready")
    three = await _loop(db_session, producer.id, "Three", "ready")
    await _like(db_session, one, 1)
    await _like(db_session, three, 3)

    resp = await client.get(
        "/api/v1/producer/loops?sort=most_liked", headers=_headers(producer)
    )

    assert resp.status_code == 200
    titles = [i["title"] for i in resp.json()["data"]["items"]]
    assert titles == ["Three", "One", "Zero"]


@pytest.mark.asyncio
async def test_the_public_listing_does_not_carry_it(client, db_session):
    producer = await _producer(db_session)
    await _loop(db_session, producer.id, "Public", "ready")

    resp = await client.get("/api/v1/loops", headers=_headers(producer))

    assert resp.status_code == 200
    assert "like_count" not in resp.json()["data"]["items"][0]
