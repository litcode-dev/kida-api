"""Thumbnail keys are content-addressed.

They used to be derived from the item id and content type alone, so replacing a
JPEG with another JPEG produced the identical CloudFront URL and the CDN went on
serving the old picture. Mixing the bytes into the key means a different image is
always a different URL.
"""
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.drum_kit import DrumKit
from app.models.loop import Genre, Loop, TempoFeel
from app.models.user import User, UserRole
from app.schemas.drum_kit import DrumKitUpdate
from app.schemas.loop import LoopUpdate
from app.services import drone_service, drum_kit_service, loop_service, s3_service


class _Upload:
    """Stand-in for a FastAPI UploadFile."""

    def __init__(self, content: bytes, content_type="image/jpeg"):
        self._content = content
        self.content_type = content_type

    async def read(self):
        return self._content


class _S3:
    """Records what the service would put in and take out of the bucket."""

    def __init__(self):
        self.uploaded, self.deleted = [], []

    def patches(self, *modules):
        return [
            p for module in modules for p in (
                patch(f"app.services.{module}.s3_service.upload_bytes", new=self._up),
                patch(f"app.services.{module}.s3_service.delete_object", new=self._del),
            )
        ]

    async def _up(self, key, body, content_type):
        self.uploaded.append(key)

    async def _del(self, key):
        self.deleted.append(key)


async def _user(db):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@test.com",
        password_hash="x", full_name="P", role=UserRole.producer, is_verified=True,
    )
    db.add(user)
    await db.commit()
    return user


# ── The key helper ───────────────────────────────────────────────────────────

def test_digest_follows_the_bytes():
    assert s3_service.content_digest(b"A") == s3_service.content_digest(b"A")
    assert s3_service.content_digest(b"A") != s3_service.content_digest(b"B")


def test_same_image_same_key_different_image_different_key():
    a, b = s3_service.content_digest(b"A"), s3_service.content_digest(b"B")
    assert s3_service.s3_key_for_loop_thumbnail("L", "jpeg", a) \
        != s3_service.s3_key_for_loop_thumbnail("L", "jpeg", b)
    assert s3_service.s3_key_for_loop_thumbnail("L", "jpeg", a) \
        == s3_service.s3_key_for_loop_thumbnail("L", "jpeg", a)


def test_keys_without_a_digest_keep_the_historical_shape():
    """Rows written before this change must still resolve."""
    assert s3_service.s3_key_for_loop_thumbnail("L", "jpeg") == "thumbnails/L_thumbnail.jpeg"
    assert s3_service.s3_key_for_drone_thumbnail("D", "jpg") == "drones/thumbnails/D_thumbnail.jpg"
    assert s3_service.s3_key_for_drum_kit_thumbnail("K", "png") == (
        "drum-kits/thumbnails/K_thumbnail.png"
    )


# ── Loops ────────────────────────────────────────────────────────────────────

async def _loop(db, user_id, thumb=None):
    loop = Loop(
        id=uuid.uuid4(), title="L", slug=f"l-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat, bpm=100, duration=5, tempo_feel=TempoFeel.mid,
        tags=[], price=Decimal("0.00"), is_free=True, is_paid=False,
        created_by=user_id, status="ready", file_s3_key="loops/x.enc",
        thumbnail_s3_key=thumb,
    )
    db.add(loop)
    await db.commit()
    await db.refresh(loop)
    return loop


@pytest.mark.asyncio
async def test_replacing_a_loop_thumbnail_changes_the_key(db_session):
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)
    s3 = _S3()

    for patcher in s3.patches("loop_service"):
        patcher.start()
    try:
        await loop_service.update_loop(
            db_session, loop.id, LoopUpdate(), thumbnail=_Upload(b"FIRST"),
        )
        first = loop.thumbnail_s3_key
        await loop_service.update_loop(
            db_session, loop.id, LoopUpdate(), thumbnail=_Upload(b"SECOND"),
        )
    finally:
        for patcher in s3.patches("loop_service"):
            patcher.stop()

    assert loop.thumbnail_s3_key != first
    # The superseded object is cleaned up, not left behind.
    assert first in s3.deleted


@pytest.mark.asyncio
async def test_re_uploading_the_identical_image_keeps_the_key(db_session):
    """And must not delete the object it just wrote."""
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)
    s3 = _S3()

    for patcher in s3.patches("loop_service"):
        patcher.start()
    try:
        await loop_service.update_loop(
            db_session, loop.id, LoopUpdate(), thumbnail=_Upload(b"SAME"),
        )
        first = loop.thumbnail_s3_key
        s3.deleted.clear()
        await loop_service.update_loop(
            db_session, loop.id, LoopUpdate(), thumbnail=_Upload(b"SAME"),
        )
    finally:
        for patcher in s3.patches("loop_service"):
            patcher.stop()

    assert loop.thumbnail_s3_key == first
    assert s3.deleted == []


@pytest.mark.asyncio
async def test_the_old_object_is_removed_only_after_the_upload(db_session):
    """Deleting first left the row pointing at a missing object when the upload
    then failed."""
    user = await _user(db_session)
    loop = await _loop(db_session, user.id, thumb="thumbnails/old_thumbnail.jpeg")
    deleted = []

    async def _explode(key, body, content_type):
        raise RuntimeError("S3 is down")

    async def _capture(key):
        deleted.append(key)

    with patch("app.services.loop_service.s3_service.upload_bytes", new=_explode), \
         patch("app.services.loop_service.s3_service.delete_object", new=_capture):
        with pytest.raises(RuntimeError):
            await loop_service.update_loop(
                db_session, loop.id, LoopUpdate(), thumbnail=_Upload(b"NEW"),
            )

    assert deleted == []
    await db_session.refresh(loop)
    assert loop.thumbnail_s3_key == "thumbnails/old_thumbnail.jpeg"


# ── Drum kits ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replacing_a_drum_kit_thumbnail_changes_the_key(db_session):
    user = await _user(db_session)
    kit = DrumKit(
        id=uuid.uuid4(), title="K", slug=f"k-{uuid.uuid4().hex[:8]}",
        is_free=True, tags=[], created_by=user.id,
    )
    db_session.add(kit)
    await db_session.commit()
    s3 = _S3()

    for patcher in s3.patches("drum_kit_service"):
        patcher.start()
    try:
        await drum_kit_service.update_drum_kit(
            db_session, kit.id, DrumKitUpdate(), thumbnail=_Upload(b"FIRST"),
        )
        first = kit.thumbnail_s3_key
        await drum_kit_service.update_drum_kit(
            db_session, kit.id, DrumKitUpdate(), thumbnail=_Upload(b"SECOND"),
        )
    finally:
        for patcher in s3.patches("drum_kit_service"):
            patcher.stop()

    assert kit.thumbnail_s3_key != first
    assert first in s3.deleted


# ── Drones ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replacing_a_drone_thumbnail_changes_the_key(db_session):
    from app.schemas.drone_pad import DronePadUpdate

    user = await _user(db_session)
    drone = Drone(
        id=uuid.uuid4(), title="D", price=Decimal("0.00"), is_free=True,
        created_by=user.id,
    )
    db_session.add(drone)
    await db_session.flush()
    db_session.add(DronePad(
        id=uuid.uuid4(), drone_id=drone.id, key=MusicalKey.C, duration=30,
        status="ready", file_s3_key="drones/c.enc",
    ))
    await db_session.commit()
    s3 = _S3()

    for patcher in s3.patches("drone_service"):
        patcher.start()
    try:
        await drone_service.update_drone(
            db_session, drone.id, DronePadUpdate(), thumbnail=_Upload(b"FIRST"),
        )
        first = s3.uploaded[-1]
        await drone_service.update_drone(
            db_session, drone.id, DronePadUpdate(), thumbnail=_Upload(b"SECOND"),
        )
    finally:
        for patcher in s3.patches("drone_service"):
            patcher.stop()

    assert s3.uploaded[-1] != first
    assert first in s3.deleted
