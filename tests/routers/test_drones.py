import pytest
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password
from app.services import cache_service


def test_ttl_drone_list_is_defined():
    assert cache_service.TTL_DRONE_LIST == 300


async def _create_user(db, role=UserRole.user):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Test",
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


async def _create_drone(
    db,
    user_id,
    title="Dark Piano Pad",
    key=MusicalKey.C,
    is_free=True,
    status="ready",
):
    drone = Drone(
        id=uuid.uuid4(),
        title=title,
        price=Decimal("0.00"),
        is_free=is_free,
        created_by=user_id,
    )
    db.add(drone)
    await db.flush()

    pad = DronePad(
        id=uuid.uuid4(),
        drone_id=drone.id,
        key=key,
        duration=30,
        status=status,
    )
    db.add(pad)
    await db.commit()
    pad.drone = drone
    return pad


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


@pytest.mark.asyncio
async def test_list_drones_returns_parent_drones(client, db_session):
    user = await _create_user(db_session)
    pad = await _create_drone(
        db_session, user.id, title="Dark Piano Pad", key=MusicalKey.C
    )

    # Bypass the cache so the assertions read this test's own data rather
    # than whatever a previous run left in Redis under the same key.
    async def _miss(key, fetch_fn, ttl):
        return await fetch_fn()

    with patch("app.routers.drones.cache_service.get_or_set", new=_miss):
        resp = await client.get("/api/v1/drones", headers=_headers(user))

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["id"] == str(pad.drone_id)
    assert data["items"][0]["title"] == "Dark Piano Pad"
    assert data["items"][0]["pads"][0]["id"] == str(pad.id)


@pytest.mark.asyncio
async def test_list_drones_caches_under_the_expected_key_and_ttl(client, db_session):
    user = await _create_user(db_session)
    await _create_drone(db_session, user.id, title="Cello Pad")

    payload = {"items": [], "total": 0, "page": 1, "page_size": 50}
    with patch(
        "app.routers.drones.cache_service.get_or_set",
        new=AsyncMock(return_value=payload),
    ) as mock_cache:
        resp = await client.get("/api/v1/drones", headers=_headers(user))

    assert resp.status_code == 200
    # Whatever the cache returns is served straight through (the hit path).
    assert resp.json()["data"] == payload
    key, _fetch, ttl = mock_cache.call_args[0]
    assert key == "drone:list:none:none:none:1:50"
    assert ttl == cache_service.TTL_DRONE_LIST


@pytest.mark.asyncio
async def test_list_drones_builds_payload_from_db_on_miss(client, db_session):
    user = await _create_user(db_session)
    pad = await _create_drone(db_session, user.id, title="Cello Pad")

    # Stand in for a cache miss: run the fetch callback get_or_set was handed.
    async def _miss(key, fetch_fn, ttl):
        return await fetch_fn()

    with patch("app.routers.drones.cache_service.get_or_set", new=_miss):
        resp = await client.get("/api/v1/drones", headers=_headers(user))

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["id"] == str(pad.drone_id)
    assert data["items"][0]["title"] == "Cello Pad"


@pytest.mark.asyncio
async def test_list_drones_cache_key_encodes_filters(client, db_session):
    user = await _create_user(db_session)
    with patch(
        "app.routers.drones.cache_service.get_or_set",
        new=AsyncMock(return_value={"items": [], "total": 0, "page": 2, "page_size": 10}),
    ) as mock_cache:
        resp = await client.get(
            "/api/v1/drones?key=A&is_free=true&page=2&page_size=10",
            headers=_headers(user),
        )

    assert resp.status_code == 200
    assert mock_cache.call_args[0][0] == "drone:list:A:true:none:2:10"


@pytest.mark.asyncio
async def test_get_drone_returns_pads(client, db_session):
    user = await _create_user(db_session)
    pad = await _create_drone(
        db_session, user.id, title="Dark Piano Pad", key=MusicalKey.C
    )

    resp = await client.get(f"/api/v1/drones/{pad.drone_id}", headers=_headers(user))

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["id"] == str(pad.drone_id)
    assert data["pads"][0]["id"] == str(pad.id)
    assert data["pads"][0]["key"] == MusicalKey.C.value


@pytest.mark.asyncio
async def test_download_drone_returns_signed_urls(client, db_session):
    user = await _create_user(db_session)
    pad = await _create_drone(
        db_session, user.id, title="Dark Piano Pad", key=MusicalKey.C
    )
    pad.file_s3_key = "drones/fake-key.wav"
    await db_session.commit()

    token = create_access_token(str(user.id), user.role.value)
    with patch(
        "app.services.s3_service.get_download_url",
        new=AsyncMock(return_value="https://signed.url/file.wav"),
    ):
        resp = await client.get(
            f"/api/v1/drones/{pad.drone_id}/download",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["drone_id"] == str(pad.drone_id)
    assert data["items"][0]["drone_pad_id"] == str(pad.id)
    assert data["items"][0]["signed_url"] == "https://signed.url/file.wav"
    assert data["items"][0]["expires_in_seconds"] == 900


@pytest.mark.asyncio
async def test_download_drone_requires_auth(client, db_session):
    user = await _create_user(db_session)
    pad = await _create_drone(db_session, user.id)

    resp = await client.get(f"/api/v1/drones/{pad.drone_id}/download")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_download_drone_excludes_unpurchased_paid_drone(client, db_session):
    user = await _create_user(db_session)
    pad = await _create_drone(
        db_session, user.id, title="Paid Cello Pad", key=MusicalKey.C, is_free=False
    )
    pad.file_s3_key = "drones/paid-key.wav"
    await db_session.commit()

    token = create_access_token(str(user.id), user.role.value)
    resp = await client.get(
        f"/api/v1/drones/{pad.drone_id}/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_download_drone_increments_download_count(client, db_session):
    user = await _create_user(db_session)
    pad = await _create_drone(
        db_session, user.id, title="Count Test Pad", key=MusicalKey.C
    )
    pad.file_s3_key = "drones/count-key.wav"
    await db_session.commit()

    token = create_access_token(str(user.id), user.role.value)
    with patch(
        "app.services.s3_service.get_download_url",
        new=AsyncMock(return_value="https://signed.url/file.wav"),
    ):
        resp = await client.get(
            f"/api/v1/drones/{pad.drone_id}/download",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200

    # pad.drone is a lazy relationship and is not loaded on this session after
    # the request, so re-read the row directly.
    drone = await db_session.get(Drone, pad.drone_id)
    await db_session.refresh(drone)
    assert drone.download_count == 1
