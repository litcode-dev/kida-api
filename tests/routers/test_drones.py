import pytest
import uuid
from decimal import Decimal
from unittest.mock import patch, AsyncMock
from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.user import User, UserRole
from app.services.auth_service import hash_password, create_access_token


async def _create_user(db, role=UserRole.user):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"), full_name="Test", role=role,
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
        id=uuid.uuid4(), title=title,
        price=Decimal("0.00"), is_free=is_free,
        created_by=user_id,
    )
    db.add(drone)
    await db.flush()
    pad = DronePad(
        id=uuid.uuid4(), drone_id=drone.id, key=key,
        duration=30, status=status,
    )
    db.add(pad)
    await db.commit()
    pad.drone = drone
    return pad


@pytest.mark.asyncio
async def test_list_drones_grouped_by_title(client, db_session):
    user = await _create_user(db_session)
    # Two drones same title (different keys) + one with different title
    await _create_drone(db_session, user.id, title="Dark Piano Pad", key=MusicalKey.C)
    await _create_drone(db_session, user.id, title="Dark Piano Pad", key=MusicalKey.D)
    await _create_drone(db_session, user.id, title="Jazz Guitar Loop", key=MusicalKey.A)

    resp = await client.get("/api/v1/drones/titles")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2  # 2 distinct titles
    titles = [item["title"] for item in data["items"]]
    assert "Dark Piano Pad" in titles
    assert "Jazz Guitar Loop" in titles
    piano_group = next(i for i in data["items"] if i["title"] == "Dark Piano Pad")
    assert len(piano_group["drones"]) == 2


@pytest.mark.asyncio
async def test_list_drones_by_title_excludes_processing(client, db_session):
    user = await _create_user(db_session)
    await _create_drone(db_session, user.id, title="Cello Pad", status="processing")
    resp = await client.get("/api/v1/drones/titles")
    assert resp.status_code == 200
    data = resp.json()["data"]
    titles = [item["title"] for item in data["items"]]
    assert "Cello Pad" not in titles


@pytest.mark.asyncio
async def test_download_by_title_returns_signed_urls(client, db_session):
    user = await _create_user(db_session)
    drone = await _create_drone(
        db_session, user.id, title="Dark Piano Pad", key=MusicalKey.C,
        is_free=True, status="ready"
    )
    drone.file_s3_key = "drones/fake-key.wav"
    await db_session.commit()

    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    with patch(
        "app.services.s3_service.get_download_url",
        new=AsyncMock(return_value="https://signed.url/file.wav"),
    ):
        resp = await client.get(
            "/api/v1/drones/titles/Dark Piano Pad/download",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["title"] == "Dark Piano Pad"
    assert data["items"][0]["signed_url"] == "https://signed.url/file.wav"
    assert data["items"][0]["expires_in_seconds"] == 900


@pytest.mark.asyncio
async def test_download_by_title_requires_auth(client):
    resp = await client.get("/api/v1/drones/titles/Dark Piano Pad/download")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_download_by_title_not_found(client, db_session):
    user = await _create_user(db_session)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    resp = await client.get(
        "/api/v1/drones/titles/NonExistentTitle/download",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_by_title_excludes_unpurchased_paid_drone(client, db_session):
    user = await _create_user(db_session)
    drone = await _create_drone(
        db_session, user.id, title="Paid Cello Pad", key=MusicalKey.C, is_free=False, status="ready"
    )
    drone.file_s3_key = "drones/paid-key.wav"
    await db_session.commit()

    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    resp = await client.get(
        "/api/v1/drones/titles/Paid Cello Pad/download",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_download_by_title_increments_download_count(client, db_session):
    from sqlalchemy import select as sa_select
    user = await _create_user(db_session)
    drone = await _create_drone(
        db_session, user.id, title="Count Test Pad", key=MusicalKey.C,
        is_free=True, status="ready"
    )
    drone.file_s3_key = "drones/count-key.wav"
    await db_session.commit()

    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    with patch(
        "app.services.s3_service.get_download_url",
        new=AsyncMock(return_value="https://signed.url/file.wav"),
    ):
        resp = await client.get(
            "/api/v1/drones/titles/Count Test Pad/download",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200

    await db_session.refresh(drone)
    await db_session.refresh(drone.drone)
    assert drone.drone.download_count == 1
