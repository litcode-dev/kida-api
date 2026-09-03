import asyncio
import boto3
from celery.exceptions import MaxRetriesExceededError
from app.tasks.celery_app import celery_app


def _s3() -> boto3.client:
    """Return a module-level S3 client, created once per worker process.

    Built by the same factory the API uses, so the worker cannot end up writing
    to a different store than the one the API reads. Still constructed lazily
    here rather than imported ready-made: a client built before Celery forks
    would have its connection pool shared across worker processes.
    """
    if not hasattr(_s3, "_client"):
        from app.services.s3_service import build_content_client
        _s3._client = build_content_client()
    return _s3._client


async def _invalidate(*keys: str, patterns: tuple[str, ...] = ()) -> None:
    """Drop cache entries after a job changes what a listing would return.

    Never allowed to fail the job. These calls sit inside the task's retry
    block, so an unguarded Redis outage would retry work that already
    succeeded and eventually mark finished audio as failed — losing an upload
    over a cache that was only ever an optimisation.
    """
    import structlog

    from app.services import cache_service

    try:
        for key in keys:
            await cache_service.delete(key)
        for pattern in patterns:
            await cache_service.delete_pattern(pattern)
    except Exception as exc:  # noqa: BLE001 - a cold cache beats a lost upload
        structlog.get_logger().warning(
            "cache_invalidation_failed", keys=keys, patterns=patterns, error=str(exc)
        )


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def process_loop_upload(self, loop_id: str):
    async def _run():
        import uuid
        import io
        import soundfile as sf
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from sqlalchemy.pool import NullPool
        from app.models.loop import Loop
        from app.services import encryption_service
        from app.services.s3_service import (
            s3_key_for_raw_loop,
            s3_key_for_encrypted_loop,
            s3_key_for_loop_preview,
        )
        from app.utils.ffmpeg_helpers import generate_preview_mp3
        from app.config import get_settings

        settings = get_settings()
        db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(db_url, poolclass=NullPool)
        SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        s3 = _s3()

        try:
            async with SessionLocal() as db:
                loop = await db.get(Loop, uuid.UUID(loop_id))
                if not loop:
                    return

                try:
                    raw_key = s3_key_for_raw_loop(loop_id)
                    obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=raw_key)
                    wav_bytes = obj["Body"].read()

                    preview_mp3 = generate_preview_mp3(wav_bytes)
                    aes_key, aes_iv = encryption_service.generate_key_and_iv()
                    encrypted_wav = encryption_service.encrypt_bytes(wav_bytes, aes_key, aes_iv)

                    enc_key = s3_key_for_encrypted_loop(loop_id)
                    prev_key = s3_key_for_loop_preview(loop_id)

                    s3.put_object(Bucket=settings.s3_bucket_name, Key=enc_key, Body=encrypted_wav, ContentType="application/octet-stream")
                    s3.put_object(Bucket=settings.s3_bucket_name, Key=prev_key, Body=preview_mp3, ContentType="audio/mpeg")

                    audio, sr = sf.read(io.BytesIO(wav_bytes))
                    duration = int(len(audio) / sr)

                    loop.file_s3_key = enc_key
                    loop.preview_s3_key = prev_key
                    loop.aes_key = aes_key
                    loop.aes_iv = aes_iv
                    loop.duration = duration
                    loop.status = "ready"
                    await db.commit()

                    from app.tasks.notification_tasks import send_new_content_push
                    send_new_content_push.delay(loop.title, "loop", loop_id)

                    s3.delete_object(Bucket=settings.s3_bucket_name, Key=raw_key)

                except Exception as exc:
                    try:
                        raise self.retry(exc=exc)
                    except MaxRetriesExceededError:
                        loop.status = "failed"
                        await db.commit()
                        raise
        finally:
            await engine.dispose()

    asyncio.run(_run())


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def process_drum_sample_upload(self, sample_id: str):
    async def _run():
        import uuid
        import io
        import soundfile as sf
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from sqlalchemy.pool import NullPool
        from app.models.drum_kit import DrumSample
        from app.services import encryption_service
        from app.services.s3_service import (
            s3_key_for_raw_drum_sample,
            s3_key_for_encrypted_drum_sample,
            s3_key_for_drum_sample_preview,
        )
        from app.utils.ffmpeg_helpers import generate_preview_mp3
        from app.config import get_settings

        settings = get_settings()
        db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(db_url, poolclass=NullPool)
        SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        s3 = _s3()

        try:
            async with SessionLocal() as db:
                sample = await db.get(DrumSample, uuid.UUID(sample_id))
                if not sample:
                    return

                try:
                    raw_key = s3_key_for_raw_drum_sample(sample_id)
                    obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=raw_key)
                    wav_bytes = obj["Body"].read()

                    preview_mp3 = generate_preview_mp3(wav_bytes)
                    aes_key, aes_iv = encryption_service.generate_key_and_iv()
                    encrypted_wav = encryption_service.encrypt_bytes(wav_bytes, aes_key, aes_iv)

                    enc_key = s3_key_for_encrypted_drum_sample(sample_id)
                    prev_key = s3_key_for_drum_sample_preview(sample_id)

                    s3.put_object(Bucket=settings.s3_bucket_name, Key=enc_key, Body=encrypted_wav, ContentType="application/octet-stream")
                    s3.put_object(Bucket=settings.s3_bucket_name, Key=prev_key, Body=preview_mp3, ContentType="audio/mpeg")

                    audio, sr = sf.read(io.BytesIO(wav_bytes))
                    duration = int(len(audio) / sr)

                    sample.file_s3_key = enc_key
                    sample.preview_s3_key = prev_key
                    sample.aes_key = aes_key
                    sample.aes_iv = aes_iv
                    sample.duration = duration
                    sample.status = "ready"
                    kit_id = sample.drum_kit_id
                    await db.commit()

                    s3.delete_object(Bucket=settings.s3_bucket_name, Key=raw_key)

                    await _invalidate(
                        f"drum_kit:detail:{kit_id}", patterns=("drum_kit:list:*",)
                    )

                    from app.models.drum_kit import DrumKit, DrumSample as _DS
                    from sqlalchemy import select as _select
                    kit = await db.get(DrumKit, kit_id)
                    all_samples = (await db.scalars(
                        _select(_DS).where(_DS.drum_kit_id == kit_id)
                    )).all()
                    if kit and all(s.status == "ready" for s in all_samples):
                        from app.tasks.notification_tasks import send_new_content_push
                        send_new_content_push.delay(kit.title, "drum_kit", str(kit_id))

                except Exception as exc:
                    try:
                        raise self.retry(exc=exc)
                    except MaxRetriesExceededError:
                        sample.status = "failed"
                        await db.commit()
                        raise
        finally:
            await engine.dispose()

    asyncio.run(_run())


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def process_stem_upload(self, stem_id: str):
    """Encrypt one uploaded stem, cut its preview and record its duration."""
    async def _run():
        import uuid
        import io
        import soundfile as sf
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from sqlalchemy.pool import NullPool
        from app.models.stem_pack import Stem
        from app.services import encryption_service
        from app.services.s3_service import (
            s3_key_for_raw_stem,
            s3_key_for_encrypted_stem,
            s3_key_for_stem_preview,
        )
        from app.utils.ffmpeg_helpers import generate_preview_mp3
        from app.config import get_settings

        settings = get_settings()
        db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(db_url, poolclass=NullPool)
        SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        s3 = _s3()

        try:
            async with SessionLocal() as db:
                stem = await db.get(Stem, uuid.UUID(stem_id))
                if not stem:
                    return

                try:
                    raw_key = s3_key_for_raw_stem(stem_id)
                    obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=raw_key)
                    wav_bytes = obj["Body"].read()

                    preview_mp3 = generate_preview_mp3(wav_bytes)
                    aes_key, aes_iv = encryption_service.generate_key_and_iv()
                    encrypted_wav = encryption_service.encrypt_bytes(wav_bytes, aes_key, aes_iv)

                    enc_key = s3_key_for_encrypted_stem(stem_id)
                    prev_key = s3_key_for_stem_preview(stem_id)

                    s3.put_object(Bucket=settings.s3_bucket_name, Key=enc_key, Body=encrypted_wav, ContentType="application/octet-stream")
                    s3.put_object(Bucket=settings.s3_bucket_name, Key=prev_key, Body=preview_mp3, ContentType="audio/mpeg")

                    audio, sr = sf.read(io.BytesIO(wav_bytes))
                    duration = int(len(audio) / sr)

                    stem.file_s3_key = enc_key
                    stem.preview_s3_key = prev_key
                    stem.aes_key = aes_key
                    stem.aes_iv = aes_iv
                    stem.duration = duration
                    stem.status = "ready"
                    pack_id = stem.stem_pack_id
                    await db.commit()

                    s3.delete_object(Bucket=settings.s3_bucket_name, Key=raw_key)

                    from app.models.stem_pack import Stem as _Stem, StemPack
                    from sqlalchemy import select as _select
                    pack = await db.get(StemPack, pack_id)
                    all_stems = (await db.scalars(
                        _select(_Stem).where(_Stem.stem_pack_id == pack_id)
                    )).all()
                    if pack and all(s.status == "ready" for s in all_stems):
                        from app.tasks.notification_tasks import send_new_content_push
                        send_new_content_push.delay(pack.title, "stem_pack", str(pack_id))

                except Exception as exc:
                    try:
                        raise self.retry(exc=exc)
                    except MaxRetriesExceededError:
                        stem.status = "failed"
                        await db.commit()
                        raise
        finally:
            await engine.dispose()

    asyncio.run(_run())


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def process_drone_upload(self, drone_id: str):
    async def _run():
        import uuid
        import io
        import soundfile as sf
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from sqlalchemy.pool import NullPool
        from app.models.drone_pad import DronePad
        from app.services import encryption_service
        from app.services.s3_service import (
            s3_key_for_raw_drone,
            s3_key_for_encrypted_drone,
            s3_key_for_drone_preview,
        )
        from app.utils.ffmpeg_helpers import generate_preview_mp3
        from app.config import get_settings

        settings = get_settings()
        db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(db_url, poolclass=NullPool)
        SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        s3 = _s3()

        try:
            async with SessionLocal() as db:
                pad = await db.get(DronePad, uuid.UUID(drone_id))
                if not pad:
                    return

                try:
                    raw_key = s3_key_for_raw_drone(drone_id)
                    obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=raw_key)
                    wav_bytes = obj["Body"].read()

                    preview_mp3 = generate_preview_mp3(wav_bytes)
                    aes_key, aes_iv = encryption_service.generate_key_and_iv()
                    encrypted_wav = encryption_service.encrypt_bytes(wav_bytes, aes_key, aes_iv)

                    enc_key = s3_key_for_encrypted_drone(drone_id)
                    prev_key = s3_key_for_drone_preview(drone_id)

                    s3.put_object(Bucket=settings.s3_bucket_name, Key=enc_key, Body=encrypted_wav, ContentType="application/octet-stream")
                    s3.put_object(Bucket=settings.s3_bucket_name, Key=prev_key, Body=preview_mp3, ContentType="audio/mpeg")

                    audio, sr = sf.read(io.BytesIO(wav_bytes))
                    duration = int(len(audio) / sr)

                    pad.file_s3_key = enc_key
                    pad.preview_s3_key = prev_key
                    if settings.s3_cloudfront_url:
                        pad.preview_url = f"{settings.s3_cloudfront_url.rstrip('/')}/{prev_key}"
                    else:
                        pad.preview_url = prev_key
                    pad.aes_key = aes_key
                    pad.aes_iv = aes_iv
                    pad.duration = duration
                    pad.status = "ready"
                    drone_id_val = pad.drone_id
                    await db.commit()

                    s3.delete_object(Bucket=settings.s3_bucket_name, Key=raw_key)

                    # The public list only shows drones whose pads are all
                    # ready, and this is where that becomes true — without
                    # this the drone stays invisible until the entry expires.
                    await _invalidate(patterns=("drone:list:*",))

                    from app.models.drone_pad import Drone as _Drone, DronePad as _DP
                    from sqlalchemy import select as _select
                    drone = await db.get(_Drone, drone_id_val)
                    all_pads = (await db.scalars(
                        _select(_DP).where(_DP.drone_id == drone_id_val)
                    )).all()
                    if drone and all(p.status == "ready" for p in all_pads):
                        from app.tasks.notification_tasks import send_new_content_push
                        send_new_content_push.delay(drone.title, "drone_pad", str(drone_id_val))

                except Exception as exc:
                    try:
                        raise self.retry(exc=exc)
                    except MaxRetriesExceededError:
                        pad.status = "failed"
                        await db.commit()
                        raise
        finally:
            await engine.dispose()

    asyncio.run(_run())
