import asyncio
import boto3
from celery.exceptions import MaxRetriesExceededError
from app.tasks.celery_app import celery_app


def _s3() -> boto3.client:
    """Return a module-level S3 client, created once per worker process."""
    if not hasattr(_s3, "_client"):
        from app.config import get_settings
        s = get_settings()
        _s3._client = boto3.client(
            "s3",
            aws_access_key_id=s.aws_access_key_id,
            aws_secret_access_key=s.aws_secret_access_key,
            region_name=s.aws_region,
        )
    return _s3._client


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

                    from app.services import cache_service as _cache
                    await _cache.delete(f"drum_kit:detail:{kit_id}")
                    await _cache.delete_pattern("drum_kit:list:*")

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
                drone = await db.get(DronePad, uuid.UUID(drone_id))
                if not drone:
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

                    drone.file_s3_key = enc_key
                    drone.preview_s3_key = prev_key
                    drone.aes_key = aes_key
                    drone.aes_iv = aes_iv
                    drone.duration = duration
                    drone.status = "ready"
                    await db.commit()

                    s3.delete_object(Bucket=settings.s3_bucket_name, Key=raw_key)

                except Exception as exc:
                    try:
                        raise self.retry(exc=exc)
                    except MaxRetriesExceededError:
                        drone.status = "failed"
                        await db.commit()
                        raise
        finally:
            await engine.dispose()

    asyncio.run(_run())
