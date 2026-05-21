import pytest
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.drum_kit import DrumKit, DrumSample
from app.models.loop import Loop, Genre, TempoFeel
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


async def _create_drone(db, user_id, title="Test Drone"):
    drone = Drone(
        id=uuid.uuid4(),
        title=title,
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


async def _create_loop(db, user_id):
    loop = Loop(
        id=uuid.uuid4(),
        title="Test Loop",
        slug=f"test-loop-{uuid.uuid4().hex[:8]}",
        genre=Genre.hiphop,
        bpm=90,
        duration=4,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("0.00"),
        is_free=True,
        is_paid=False,
        created_by=user_id,
    )
    db.add(loop)
    await db.commit()
    return loop


async def _create_drum_kit(db, user_id):
    kit_id = uuid.uuid4()
    kit = DrumKit(
        id=kit_id,
        title="Test Kit",
        slug=f"test-kit-{str(kit_id)[:8]}",
        is_free=True,
        tags=[],
        created_by=user_id,
    )
    db.add(kit)
    await db.flush()
    sample = DrumSample(
        id=uuid.uuid4(),
        drum_kit_id=kit.id,
        label="Kick",
        status="ready",
    )
    db.add(sample)
    await db.commit()
    return kit, sample


# --- Drone PUT tests ---

@pytest.mark.asyncio
async def test_update_drone_metadata_via_multipart(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    pad = await _create_drone(db_session, user.id, title="Old Title")
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    with patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()):
        resp = await client.put(
            f"/api/v1/admin/drones/{pad.drone_id}",
            data={"title": "New Title"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "New Title"


@pytest.mark.asyncio
async def test_update_drone_with_thumbnail_uploads_to_s3(client, db_session):
    user = await _create_user(db_session, role=UserRole.admin)
    pad = await _create_drone(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    with patch("app.services.drone_service.s3_service.upload_bytes", new=AsyncMock()) as mock_upload, \
         patch("app.services.drone_service.s3_service.delete_object", new=AsyncMock()), \
         patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()):
        resp = await client.put(
            f"/api/v1/admin/drones/{pad.drone_id}",
            files={"thumbnail": ("thumb.jpg", b"fake-image-bytes", "image/jpeg")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    mock_upload.assert_awaited_once()


# --- Drone PATCH /pads/{pad_id} tests ---

@pytest.mark.asyncio
async def test_replace_drone_pad_audio_sets_processing_and_queues_celery(client, db_session):
    user = await _create_user(db_session, role=UserRole.producer)
    pad = await _create_drone(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    mock_task = MagicMock()
    with patch("app.services.drone_service.validate_wav_upload", new=AsyncMock(return_value=b"fakewav")), \
         patch("app.services.drone_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.drone_service.s3_service.delete_object", new=AsyncMock()), \
         patch("app.routers.admin.cache_service.delete_pattern", new=AsyncMock()), \
         patch("app.routers.admin.process_drone_upload", mock_task):
        resp = await client.patch(
            f"/api/v1/admin/drones/{pad.drone_id}/pads/{pad.id}",
            files={"file": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "processing"
    mock_task.delay.assert_called_once_with(str(pad.id))


@pytest.mark.asyncio
async def test_replace_drone_pad_audio_returns_404_if_pad_not_in_drone(client, db_session):
    user = await _create_user(db_session, role=UserRole.producer)
    pad = await _create_drone(db_session, user.id)
    token = create_access_token({"sub": str(user.id), "role": user.role.value})
    wrong_drone_id = uuid.uuid4()

    with patch("app.services.drone_service.validate_wav_upload", new=AsyncMock(return_value=b"fakewav")), \
         patch("app.services.drone_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.drone_service.s3_service.delete_object", new=AsyncMock()):
        resp = await client.patch(
            f"/api/v1/admin/drones/{wrong_drone_id}/pads/{pad.id}",
            files={"file": ("test.wav", b"RIFF....", "audio/wav")},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 404
