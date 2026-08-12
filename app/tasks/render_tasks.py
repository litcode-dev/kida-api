"""Rendering a breakdown pack's arrangement into downloadable tracks.

The producer uploads a song part by part; an arrangement says what order the
parts play in. This job walks that running order once per instrument and
stitches the parts back into a single continuous file, so the buyer downloads
a finished song rather than a folder of fragments they have to line up
themselves.
"""
import asyncio

from celery.exceptions import MaxRetriesExceededError

from app.tasks.celery_app import celery_app
from app.tasks.upload_tasks import _s3


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def render_stem_arrangement(self, arrangement_id: str):
    async def _run():
        import uuid
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from sqlalchemy.orm import selectinload
        from sqlalchemy.pool import NullPool

        from app.config import get_settings
        from app.models.stem_pack import Stem, StemArrangement, StemArrangementTrack
        from app.services import encryption_service
        from app.services.s3_service import (
            s3_key_for_arrangement_track_preview,
            s3_key_for_encrypted_arrangement_track,
        )
        from app.services.stem_arrangement_service import timeline
        from app.utils.ffmpeg_helpers import (
            concat_wavs, generate_preview_mp3, silent_wav, wav_duration_seconds,
        )

        settings = get_settings()
        db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(db_url, poolclass=NullPool)
        SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        s3 = _s3()

        def fetch_wav(stem: Stem) -> bytes:
            """Pull one stem back out of S3 and decrypt it to plain WAV bytes."""
            obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=stem.file_s3_key)
            return encryption_service.decrypt_bytes(
                obj["Body"].read(), stem.aes_key, stem.aes_iv
            )

        try:
            async with SessionLocal() as db:
                arrangement = await db.scalar(
                    select(StemArrangement)
                    .options(selectinload(StemArrangement.items))
                    .where(StemArrangement.id == uuid.UUID(arrangement_id))
                )
                if not arrangement or not arrangement.items:
                    return

                try:
                    order = timeline(arrangement.items)
                    part_ids = list(dict.fromkeys(order))

                    stems = list((await db.scalars(
                        select(Stem).where(
                            Stem.part_id.in_(part_ids), Stem.status == "ready"
                        )
                    )).all())
                    stems = [s for s in stems if s.file_s3_key and s.aes_key and s.aes_iv]
                    if not stems:
                        raise RuntimeError("Arrangement has no ready stems to render")

                    by_part_instrument = {(s.part_id, s.instrument): s for s in stems}
                    instruments = sorted(
                        {s.instrument for s in stems}, key=lambda i: i.value
                    )

                    # An instrument that sits out a part still has to occupy that
                    # part's length, so every part's exact duration is needed up
                    # front — including parts the instrument being rendered does
                    # not play in. Measured from one representative stem and
                    # cached, rather than from the whole-second duration column,
                    # which would drift a beat over a long song.
                    part_seconds: dict[uuid.UUID, float] = {}
                    for part_id in part_ids:
                        representative = next(
                            (s for s in stems if s.part_id == part_id), None
                        )
                        if representative is None:
                            part_seconds[part_id] = 0.0
                            continue
                        part_seconds[part_id] = await asyncio.to_thread(
                            lambda s=representative: wav_duration_seconds(fetch_wav(s))
                        )

                    total_seconds = 0
                    for instrument in instruments:
                        # Only this instrument's audio is held at a time; a pack
                        # with eight instruments would otherwise sit on every
                        # part of every one of them at once.
                        cache: dict[uuid.UUID, bytes] = {}
                        segments: list[bytes] = []
                        for part_id in order:
                            stem = by_part_instrument.get((part_id, instrument))
                            if stem is None:
                                segments.append(await asyncio.to_thread(
                                    silent_wav, part_seconds.get(part_id, 0.0)
                                ))
                                continue
                            if part_id not in cache:
                                cache[part_id] = await asyncio.to_thread(
                                    lambda s=stem: fetch_wav(s)
                                )
                            segments.append(cache[part_id])

                        rendered = await asyncio.to_thread(concat_wavs, segments)
                        del cache, segments

                        preview_mp3 = await asyncio.to_thread(generate_preview_mp3, rendered)
                        aes_key, aes_iv = encryption_service.generate_key_and_iv()
                        encrypted = await asyncio.to_thread(
                            encryption_service.encrypt_bytes, rendered, aes_key, aes_iv
                        )
                        seconds = int(await asyncio.to_thread(wav_duration_seconds, rendered))
                        del rendered

                        track = await db.scalar(
                            select(StemArrangementTrack).where(
                                StemArrangementTrack.arrangement_id == arrangement.id,
                                StemArrangementTrack.instrument == instrument,
                            )
                        )
                        if track is None:
                            track = StemArrangementTrack(
                                arrangement_id=arrangement.id, instrument=instrument
                            )
                            db.add(track)
                            await db.flush()

                        enc_key = s3_key_for_encrypted_arrangement_track(str(track.id))
                        prev_key = s3_key_for_arrangement_track_preview(str(track.id))
                        s3.put_object(
                            Bucket=settings.s3_bucket_name, Key=enc_key,
                            Body=encrypted, ContentType="application/octet-stream",
                        )
                        s3.put_object(
                            Bucket=settings.s3_bucket_name, Key=prev_key,
                            Body=preview_mp3, ContentType="audio/mpeg",
                        )

                        track.file_s3_key = enc_key
                        track.preview_s3_key = prev_key
                        track.aes_key = aes_key
                        track.aes_iv = aes_iv
                        track.duration = seconds
                        track.status = "ready"
                        total_seconds = max(total_seconds, seconds)
                        await db.commit()

                    # An instrument dropped from the pack since the last render
                    # would otherwise leave a track nobody can account for.
                    stale_tracks = await db.scalars(
                        select(StemArrangementTrack).where(
                            StemArrangementTrack.arrangement_id == arrangement.id,
                            StemArrangementTrack.instrument.notin_(instruments),
                        )
                    )
                    for track in stale_tracks.all():
                        for key in (track.file_s3_key, track.preview_s3_key):
                            if key:
                                s3.delete_object(Bucket=settings.s3_bucket_name, Key=key)
                        await db.delete(track)

                    arrangement.status = "ready"
                    arrangement.duration = total_seconds
                    arrangement.rendered_at = datetime.now(timezone.utc)
                    await db.commit()

                except Exception as exc:
                    try:
                        raise self.retry(exc=exc)
                    except MaxRetriesExceededError:
                        arrangement.status = "failed"
                        await db.commit()
                        raise
        finally:
            await engine.dispose()

    asyncio.run(_run())
