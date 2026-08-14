"""Finishing an upload has to drop the cached listings it changes.

The public lists only show content whose audio is ready, and that becomes true
inside these jobs. Without invalidation the item stays invisible until the
cache entry expires — five minutes of a producer refreshing and not seeing
their own upload.
"""
import io
import shutil
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import soundfile as sf

from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.drum_kit import DrumKit, DrumSample
from app.models.user import User, UserRole
from app.services.auth_service import hash_password

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


class FakeS3:
    def __init__(self, raw: bytes):
        self.objects: dict[str, bytes] = {}
        self.raw = raw

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.raw)}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def _wav(seconds: float = 0.5, sr: int = 44100) -> bytes:
    import numpy as np

    buf = io.BytesIO()
    sf.write(buf, np.zeros((int(seconds * sr), 2), dtype="float32"), sr,
             format="WAV", subtype="PCM_16")
    return buf.getvalue()


async def _user(db):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Producer",
        role=UserRole.producer,
    )
    db.add(user)
    await db.flush()
    return user


async def _run_task(task, arg):
    """Celery tasks drive their own event loop, so they run on a worker thread."""
    import asyncio

    await asyncio.to_thread(task.apply, args=[arg], throw=True)


@pytest.mark.asyncio
async def test_finishing_a_drone_pad_drops_the_drone_list_cache(db_session, monkeypatch):
    from app.tasks import upload_tasks

    user = await _user(db_session)
    drone = Drone(
        id=uuid.uuid4(), title="Cinematic Pad", price=Decimal("0.00"),
        is_free=True, created_by=user.id,
    )
    db_session.add(drone)
    await db_session.flush()
    pad = DronePad(
        id=uuid.uuid4(), drone_id=drone.id, key=MusicalKey.C,
        duration=0, status="processing",
    )
    db_session.add(pad)
    await db_session.commit()

    monkeypatch.setattr(upload_tasks, "_s3", lambda: FakeS3(_wav()))
    with patch("app.services.cache_service.delete_pattern", new=AsyncMock()) as dropped, \
         patch("app.services.cache_service.delete", new=AsyncMock()), \
         patch("app.tasks.notification_tasks.send_new_content_push.delay"):
        await _run_task(upload_tasks.process_drone_upload, str(pad.id))

    await db_session.refresh(pad)
    assert pad.status == "ready"
    dropped.assert_any_await("drone:list:*")


@pytest.mark.asyncio
async def test_finishing_a_drum_sample_drops_the_kit_caches(db_session, monkeypatch):
    from app.tasks import upload_tasks

    user = await _user(db_session)
    kit = DrumKit(
        id=uuid.uuid4(), title="Lagos Kit", slug=uuid.uuid4().hex[:12],
        tags=[], is_free=True, created_by=user.id,
    )
    db_session.add(kit)
    await db_session.flush()
    sample = DrumSample(
        id=uuid.uuid4(), drum_kit_id=kit.id, label="Kick",
        duration=0, status="processing",
    )
    db_session.add(sample)
    await db_session.commit()

    monkeypatch.setattr(upload_tasks, "_s3", lambda: FakeS3(_wav()))
    with patch("app.services.cache_service.delete_pattern", new=AsyncMock()) as dropped_pattern, \
         patch("app.services.cache_service.delete", new=AsyncMock()) as dropped_key, \
         patch("app.tasks.notification_tasks.send_new_content_push.delay"):
        await _run_task(upload_tasks.process_drum_sample_upload, str(sample.id))

    await db_session.refresh(sample)
    assert sample.status == "ready"
    dropped_pattern.assert_any_await("drum_kit:list:*")
    dropped_key.assert_any_await(f"drum_kit:detail:{kit.id}")


@pytest.mark.asyncio
async def test_a_cache_outage_does_not_fail_a_finished_upload(db_session, monkeypatch):
    """The invalidation sits inside the task's retry block. Unguarded, a Redis
    outage would retry work that already succeeded and end up marking finished
    audio as failed — losing an upload over an optimisation."""
    from app.tasks import upload_tasks

    user = await _user(db_session)
    drone = Drone(
        id=uuid.uuid4(), title="Cinematic Pad", price=Decimal("0.00"),
        is_free=True, created_by=user.id,
    )
    db_session.add(drone)
    await db_session.flush()
    pad = DronePad(
        id=uuid.uuid4(), drone_id=drone.id, key=MusicalKey.C,
        duration=0, status="processing",
    )
    db_session.add(pad)
    await db_session.commit()

    monkeypatch.setattr(upload_tasks, "_s3", lambda: FakeS3(_wav()))
    with patch(
        "app.services.cache_service.delete_pattern",
        new=AsyncMock(side_effect=ConnectionError("redis is down")),
    ), patch("app.tasks.notification_tasks.send_new_content_push.delay"):
        await _run_task(upload_tasks.process_drone_upload, str(pad.id))

    await db_session.refresh(pad)
    assert pad.status == "ready"
