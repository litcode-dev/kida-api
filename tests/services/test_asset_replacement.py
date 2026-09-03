"""Replacing an asset must not destroy the one the row still points at.

The old object used to be deleted while the request that named its replacement
could still fail, and the audio was deleted for a re-encode that overwrites the
same key anyway. The last two tests here pin what must stay true either way: a
replaced thumbnail really is orphaned and does have to be cleaned up, and
deleting a loop still takes its objects with it.
"""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.exceptions import AppError
from app.models.loop import Loop, Genre, TempoFeel
from app.models.user import User, UserRole
from app.schemas.loop import LoopUpdate
from app.services import loop_service
from app.services.auth_service import hash_password


async def _user(db):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Test",
        role=UserRole.admin,
    )
    db.add(user)
    await db.commit()
    return user


async def _loop(db, user_id):
    loop = Loop(
        id=uuid.uuid4(),
        title="Test Loop",
        slug=f"test-loop-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        bpm=90,
        duration=4,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("0.00"),
        is_free=True,
        is_paid=False,
        file_s3_key="loops/encrypted/old.wav.enc",
        preview_s3_key="previews/old_preview.mp3",
        thumbnail_s3_key="thumbnails/old_thumbnail_abc123.jpg",
        created_by=user_id,
    )
    db.add(loop)
    await db.commit()
    return loop


def _upload(name, content, content_type):
    from fastapi import UploadFile
    import io

    return UploadFile(
        filename=name, file=io.BytesIO(content), headers={"content-type": content_type}
    )


@pytest.mark.asyncio
async def test_replacing_audio_keeps_the_object_the_reencode_overwrites(db_session):
    """The encrypted and preview keys come from the loop id, so the job that
    re-encodes the upload writes over them. Deleting them here would only leave
    a loop with no audio if that job never ran."""
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)

    with patch("app.services.loop_service.validate_wav_upload", new=AsyncMock(return_value=b"wav")), \
         patch("app.services.loop_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.loop_service.s3_service.delete_object", new=AsyncMock()) as delete:
        updated, should_reprocess = await loop_service.update_loop(
            db_session, loop.id, LoopUpdate(),
            file=_upload("new.wav", b"RIFF", "audio/wav"),
        )

    delete.assert_not_awaited()
    assert should_reprocess is True
    assert updated.status == "processing"
    # Cleared so nothing serves the old audio while the job runs.
    assert updated.file_s3_key is None
    assert updated.preview_s3_key is None


@pytest.mark.asyncio
async def test_a_failed_update_leaves_the_old_thumbnail_in_place(db_session):
    """The thumbnail is written before the audio is validated. When the audio
    is refused, the row rolls back to the old key — which has to still exist."""
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)
    # Read before the failed call: the rollback expires the instance, and any
    # attribute touched afterwards would reload it.
    loop_id, old_thumb_key = loop.id, loop.thumbnail_s3_key

    refuse = AppError("Only WAV files are accepted", status_code=422)
    with patch("app.services.loop_service.validate_wav_upload", new=AsyncMock(side_effect=refuse)), \
         patch("app.services.loop_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.loop_service.s3_service.delete_object", new=AsyncMock()) as delete:
        with pytest.raises(AppError):
            await loop_service.update_loop(
                db_session, loop_id, LoopUpdate(),
                thumbnail=_upload("t.png", b"image-bytes", "image/png"),
                file=_upload("new.mp3", b"not-a-wav", "audio/mpeg"),
            )

    delete.assert_not_awaited()
    # The write is discarded, so the row is back to naming the old object.
    await db_session.rollback()
    persisted = await db_session.scalar(
        select(Loop.thumbnail_s3_key).where(Loop.id == loop_id)
    )
    assert persisted == old_thumb_key


@pytest.mark.asyncio
async def test_a_successful_thumbnail_swap_removes_the_old_object(db_session):
    """Thumbnail keys carry a digest of their content, so a replaced one is
    orphaned rather than overwritten — it does have to be cleaned up."""
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)
    old_thumb_key = loop.thumbnail_s3_key

    with patch("app.services.loop_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.s3_service.delete_object", new=AsyncMock()) as delete:
        updated, _ = await loop_service.update_loop(
            db_session, loop.id, LoopUpdate(),
            thumbnail=_upload("t.png", b"different-image-bytes", "image/png"),
        )

    assert updated.thumbnail_s3_key != old_thumb_key
    delete.assert_awaited_once_with(old_thumb_key)


@pytest.mark.asyncio
async def test_a_stale_object_that_will_not_delete_does_not_fail_the_update(db_session):
    """The row is already committed; a leftover object is not worth a 500 that
    tells the caller their update was lost."""
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)

    boom = AsyncMock(side_effect=RuntimeError("S3 is having a day"))
    with patch("app.services.loop_service.s3_service.upload_bytes", new=AsyncMock()), \
         patch("app.services.s3_service.delete_object", new=boom):
        updated, _ = await loop_service.update_loop(
            db_session, loop.id, LoopUpdate(title="Renamed"),
            thumbnail=_upload("t.png", b"different-image-bytes", "image/png"),
        )

    assert updated.title == "Renamed"


@pytest.mark.asyncio
async def test_deleting_a_loop_removes_its_objects_after_the_row_is_gone(db_session):
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)

    with patch("app.services.s3_service.delete_object", new=AsyncMock()) as delete:
        await loop_service.delete_loop(db_session, loop.id)

    deleted = {call.args[0] for call in delete.await_args_list}
    assert "loops/encrypted/old.wav.enc" in deleted
    assert "previews/old_preview.mp3" in deleted
    assert "thumbnails/old_thumbnail_abc123.jpg" in deleted
    with pytest.raises(Exception):
        await loop_service.get_loop(db_session, loop.id)
