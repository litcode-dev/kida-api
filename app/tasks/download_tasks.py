import asyncio
from app.tasks.celery_app import celery_app


@celery_app.task
def generate_waveform_task(loop_id: str):
    async def _run():
        import uuid
        from app.database import AsyncSessionLocal
        from app.models.loop import Loop
        from app.services.waveform_service import generate_waveform
        from app.services.encryption_service import decrypt_bytes
        from app.config import get_settings

        settings = get_settings()
        async with AsyncSessionLocal() as db:
            loop = await db.get(Loop, uuid.UUID(loop_id))
            if not loop or not loop.file_s3_key:
                return
            from app.services.s3_service import build_content_client
            s3 = build_content_client()
            obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=loop.file_s3_key)
            encrypted = obj["Body"].read()
            wav_bytes = decrypt_bytes(encrypted, loop.aes_key, loop.aes_iv)
            waveform = generate_waveform(wav_bytes)
            loop.waveform_data = waveform
            await db.commit()
    asyncio.run(_run())


@celery_app.task
def cleanup_expired_downloads():
    async def _run():
        from app.database import AsyncSessionLocal
        from app.models.download import Download
        from sqlalchemy import delete
        from datetime import datetime, timezone

        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(Download).where(Download.expires_at < datetime.now(timezone.utc))
            )
            await db.commit()
    asyncio.run(_run())
