"""The admin user list: newest accounts first, held across pages."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


async def _create_user(db, role=UserRole.user, created_at=None):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Test",
        role=role,
        created_at=created_at,
    )
    db.add(user)
    await db.commit()
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _seed_dated_users(db, count):
    """Oldest first, so the endpoint has to reverse them."""
    base = datetime.now(timezone.utc) - timedelta(days=count)
    return [
        await _create_user(db, created_at=base + timedelta(days=i))
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_users_are_listed_newest_first(client, db_session):
    admin = await _create_user(db_session, role=UserRole.admin)
    users = await _seed_dated_users(db_session, 3)

    resp = await client.get("/api/v1/admin/users", headers=_headers(admin))

    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    dates = [item["created_at"] for item in items]
    assert dates == sorted(dates, reverse=True)
    # The admin was created before the seeded three, so they sit last.
    assert [i["id"] for i in items[:3]] == [str(u.id) for u in reversed(users)]


@pytest.mark.asyncio
async def test_ordering_holds_across_pages(client, db_session):
    admin = await _create_user(db_session, role=UserRole.admin)
    users = await _seed_dated_users(db_session, 4)
    expected = [str(u.id) for u in reversed(users)] + [str(admin.id)]

    seen = []
    for page in (1, 2, 3):
        resp = await client.get(
            f"/api/v1/admin/users?page={page}&page_size=2", headers=_headers(admin)
        )
        assert resp.status_code == 200
        seen += [item["id"] for item in resp.json()["data"]["items"]]

    assert seen == expected
