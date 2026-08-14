"""End-to-end cover for the arrangement renderer.

Runs the real Celery task body against the test database with S3 faked in
memory, so the parts really are decrypted, stitched and re-encrypted — the one
place where a wrong assumption produces a song that is silent, out of order or
a beat short rather than an exception.
"""
import asyncio
import io
import shutil
import uuid
from decimal import Decimal

import pytest
import soundfile as sf

from app.models.loop import Genre
from app.models.stem_pack import (
    SongSection,
    Stem,
    StemArrangement,
    StemArrangementItem,
    StemArrangementTrack,
    StemInstrument,
    StemPack,
    StemPackType,
    StemPart,
)
from app.models.user import User, UserRole
from app.services import encryption_service
from app.services.auth_service import hash_password
from app.utils.ffmpeg_helpers import wav_duration_seconds

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


class FakeS3:
    """Just enough of the boto3 client for the renderer's three calls."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


def _wav(seconds: float, sample_rate: int = 44100) -> bytes:
    import numpy as np

    audio = np.zeros((int(seconds * sample_rate), 2), dtype="float32")
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


async def _seed(db, s3: FakeS3):
    """A two-part pack: drums play throughout, piano only in the intro."""
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Producer",
        role=UserRole.producer,
    )
    db.add(user)
    await db.flush()

    pack = StemPack(
        id=uuid.uuid4(),
        title="Breakdown Pack",
        slug=f"breakdown-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        pack_type=StemPackType.breakdown,
        bpm=110,
        key="C",
        tags=[],
        price=Decimal("20.00"),
        created_by=user.id,
    )
    db.add(pack)
    await db.flush()

    intro = StemPart(
        id=uuid.uuid4(), stem_pack_id=pack.id, section=SongSection.intro,
        name="Intro", position=0,
    )
    chorus = StemPart(
        id=uuid.uuid4(), stem_pack_id=pack.id, section=SongSection.chorus,
        name="Chorus", position=1,
    )
    db.add_all([intro, chorus])
    await db.flush()

    def add_stem(part, instrument, seconds):
        stem_id = uuid.uuid4()
        key, iv = encryption_service.generate_key_and_iv()
        s3_key = f"stems/encrypted/{stem_id}.wav.enc"
        s3.objects[s3_key] = encryption_service.encrypt_bytes(_wav(seconds), key, iv)
        db.add(Stem(
            id=stem_id,
            stem_pack_id=pack.id,
            part_id=part.id,
            instrument=instrument,
            label=instrument.value,
            file_s3_key=s3_key,
            aes_key=key,
            aes_iv=iv,
            duration=int(seconds),
            status="ready",
        ))

    add_stem(intro, StemInstrument.drums, 1.0)
    add_stem(intro, StemInstrument.piano, 1.0)
    add_stem(chorus, StemInstrument.drums, 2.0)

    arrangement = StemArrangement(
        id=uuid.uuid4(),
        stem_pack_id=pack.id,
        name="Full Song",
        status="rendering",
        is_default=True,
        created_by=user.id,
    )
    arrangement.items = [
        StemArrangementItem(part_id=intro.id, position=0, repeat_count=1),
        StemArrangementItem(part_id=chorus.id, position=1, repeat_count=2),
    ]
    db.add(arrangement)
    await db.commit()
    return arrangement


@pytest.mark.asyncio
async def test_re_render_reuses_the_existing_tracks(db_session, monkeypatch):
    """A second render updates the rows the first one wrote.

    The task looks its tracks up in one query and keeps that map in step with
    what it creates; getting that wrong would either duplicate a row — which
    the per-instrument unique constraint refuses — or strand the old one.
    """
    from app.tasks import render_tasks

    s3 = FakeS3()
    arrangement = await _seed(db_session, s3)
    monkeypatch.setattr(render_tasks, "_s3", lambda: s3)

    async def _track_ids():
        rows = (await db_session.execute(
            StemArrangementTrack.__table__.select().where(
                StemArrangementTrack.arrangement_id == arrangement.id
            )
        )).mappings().all()
        return {r["instrument"]: r["id"] for r in rows}

    await asyncio.to_thread(
        render_tasks.render_stem_arrangement.apply, args=[str(arrangement.id)], throw=True
    )
    first = await _track_ids()

    arrangement.status = "rendering"
    await db_session.commit()
    await asyncio.to_thread(
        render_tasks.render_stem_arrangement.apply, args=[str(arrangement.id)], throw=True
    )
    second = await _track_ids()

    assert first == second, "re-rendering replaced the track rows instead of updating them"
    assert len(second) == 2

    await db_session.refresh(arrangement)
    assert arrangement.status == "ready"


@pytest.mark.asyncio
async def test_render_stitches_parts_and_pads_absent_instruments(db_session, monkeypatch):
    from app.tasks import render_tasks

    s3 = FakeS3()
    arrangement = await _seed(db_session, s3)
    monkeypatch.setattr(render_tasks, "_s3", lambda: s3)

    # The task drives its own event loop, which cannot be started inside this
    # one — run it on a worker thread, as Celery would.
    await asyncio.to_thread(
        render_tasks.render_stem_arrangement.apply,
        args=[str(arrangement.id)],
        throw=True,
    )

    await db_session.refresh(arrangement)
    assert arrangement.status == "ready"
    # Intro (1s) + chorus (2s) played twice.
    assert arrangement.duration == 5

    rows = (await db_session.execute(
        StemArrangementTrack.__table__.select().where(
            StemArrangementTrack.arrangement_id == arrangement.id
        )
    )).mappings().all()
    by_instrument = {r["instrument"]: r for r in rows}

    assert set(by_instrument) == {StemInstrument.drums, StemInstrument.piano}
    for row in by_instrument.values():
        assert row["status"] == "ready"
        assert row["duration"] == 5
        rendered = encryption_service.decrypt_bytes(
            s3.objects[row["file_s3_key"]], row["aes_key"], row["aes_iv"]
        )
        # The piano sits out both choruses, so its track is padded with silence
        # to the same length rather than ending after the intro.
        assert wav_duration_seconds(rendered) == pytest.approx(5.0, abs=0.15)
        assert row["preview_s3_key"] in s3.objects
