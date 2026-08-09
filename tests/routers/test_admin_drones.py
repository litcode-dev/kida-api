import pytest
import uuid
import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


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


async def _create_drone(db, user_id):
    drone = Drone(
        id=uuid.uuid4(),
        title="Test Drone",
        price=Decimal("0.00"),
        is_free=True,
        created_by=user_id,
    )
    db.add(drone)
    await db.flush()
    pad = DronePad(
        id=uuid.uuid4(),
        drone_id=drone.id,
        key=MusicalKey.C,
        duration=30,
        status="ready",
    )
    db.add(pad)
    await db.commit()
    pad.drone = drone
    return pad


def _fake_drone(user_id: uuid.UUID) -> Drone:
    drone = Drone(
        id=uuid.uuid4(),
        title="Fake Drone",
        price=Decimal("0.00"),
        is_free=True,
        created_by=user_id,
        download_count=0,
        created_at=datetime.datetime.utcnow(),
    )
    drone.pads = []
    drone.category = None
    return drone


@pytest.mark.asyncio
async def test_upload_drone_invalidates_list_cache(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    token = create_access_token(str(user.id), user.role.value)
    fake_drone = _fake_drone(user.id)

    with patch("app.routers.admin.drone_service.create_drone", new=AsyncMock(return_value=fake_drone)), \
         patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()) as mock_invalidate:
        resp = await client.post(
            "/api/v1/admin/drones",
            data={"title": "New Drone", "key": "C", "is_free": "true"},
            files={"file": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_invalidate.assert_awaited_once_with("drone:list:*")


@pytest.mark.asyncio
async def test_bulk_upload_drones_invalidates_list_cache(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    token = create_access_token(str(user.id), user.role.value)
    fake_drone = _fake_drone(user.id)

    with patch("app.routers.admin.drone_service.bulk_create_drones", new=AsyncMock(return_value=(fake_drone, []))), \
         patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()) as mock_invalidate:
        resp = await client.post(
            "/api/v1/admin/drones/bulk",
            data={"title": "Bulk Drone", "keys": "C", "is_free": "true"},
            files={"files": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_invalidate.assert_awaited_once_with("drone:list:*")


@pytest.mark.asyncio
async def test_update_drone_invalidates_list_cache(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    pad = await _create_drone(db_session, user.id)
    token = create_access_token(str(user.id), user.role.value)

    with patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()) as mock_invalidate:
        resp = await client.put(
            f"/api/v1/admin/drones/{pad.drone_id}",
            json={"title": "Updated Title"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_invalidate.assert_awaited_once_with("drone:list:*")


@pytest.mark.asyncio
async def test_delete_drone_invalidates_list_cache(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    pad = await _create_drone(db_session, user.id)
    token = create_access_token(str(user.id), user.role.value)

    with patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()) as mock_invalidate:
        resp = await client.delete(
            f"/api/v1/admin/drones/{pad.drone_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_invalidate.assert_awaited_once_with("drone:list:*")
