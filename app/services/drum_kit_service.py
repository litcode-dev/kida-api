import uuid
import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from fastapi import UploadFile
from app.models.drum_kit import DrumKit, DrumSample
from app.schemas.drum_kit import DrumKitCreate, DrumKitFilter
from app.exceptions import NotFoundError, AppError
from app.services import s3_service
from app.utils.audio_validator import validate_wav_upload

MAX_SAMPLES_PER_KIT = 50


def _slugify(title: str, uid: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return f"{slug}-{uid[:8]}"


async def create_drum_kit(
    db: AsyncSession,
    data: DrumKitCreate,
    created_by: uuid.UUID,
    sample_files: list[UploadFile],
    sample_labels: list[str],
    thumbnail: UploadFile | None = None,
) -> tuple[DrumKit, list[str]]:
    if not sample_files:
        raise AppError("At least one sample file is required", status_code=422)
    if len(sample_files) > MAX_SAMPLES_PER_KIT:
        raise AppError(f"A drum kit can have at most {MAX_SAMPLES_PER_KIT} samples", status_code=422)
    if len(sample_files) != len(sample_labels):
        raise AppError("Number of sample files must match number of labels", status_code=422)

    kit_id = str(uuid.uuid4())

    thumb_key = None
    if thumbnail:
        thumb_bytes = await thumbnail.read()
        content_type = thumbnail.content_type or "image/jpeg"
        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
        thumb_key = s3_service.s3_key_for_drum_kit_thumbnail(kit_id, ext)
        await s3_service.upload_bytes(thumb_key, thumb_bytes, content_type)

    slug = _slugify(data.title, kit_id)
    store_product_id = None if data.is_free else f"com.litmusic.drumkit.{slug}"

    kit = DrumKit(
        id=uuid.UUID(kit_id),
        title=data.title,
        slug=slug,
        description=data.description,
        thumbnail_s3_key=thumb_key,
        tags=data.tags,
        is_free=data.is_free,
        price=data.price,
        store_product_id=store_product_id,
        created_by=created_by,
    )
    db.add(kit)
    await db.flush()

    sample_ids = []
    for file, label in zip(sample_files, sample_labels):
        wav_bytes = await validate_wav_upload(file)
        sample_id = str(uuid.uuid4())
        raw_key = s3_service.s3_key_for_raw_drum_sample(sample_id)
        await s3_service.upload_bytes(raw_key, wav_bytes, "audio/wav")

        sample = DrumSample(
            id=uuid.UUID(sample_id),
            drum_kit_id=kit.id,
            label=label,
            status="processing",
        )
        db.add(sample)
        sample_ids.append(sample_id)

    await db.commit()
    await db.refresh(kit)
    return kit, sample_ids


async def get_drum_kit(db: AsyncSession, kit_id: uuid.UUID) -> DrumKit:
    kit = await db.get(DrumKit, kit_id)
    if not kit:
        raise NotFoundError(f"Drum kit {kit_id} not found")
    return kit


async def update_drum_kit(
    db: AsyncSession,
    kit_id: uuid.UUID,
    data: "DrumKitUpdate",
    thumbnail: UploadFile | None = None,
) -> DrumKit:
    from app.schemas.drum_kit import DrumKitUpdate
    kit = await get_drum_kit(db, kit_id)

    if thumbnail:
        if kit.thumbnail_s3_key:
            await s3_service.delete_object(kit.thumbnail_s3_key)
        thumb_bytes = await thumbnail.read()
        content_type = thumbnail.content_type or "image/jpeg"
        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
        new_thumb_key = s3_service.s3_key_for_drum_kit_thumbnail(str(kit_id), ext)
        await s3_service.upload_bytes(new_thumb_key, thumb_bytes, content_type)
        kit.thumbnail_s3_key = new_thumb_key

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(kit, field, value)

    await db.commit()
    await db.refresh(kit)
    return kit


async def list_drum_kits(db: AsyncSession, filters: DrumKitFilter) -> tuple[list[DrumKit], int]:
    q = select(DrumKit)
    if filters.search:
        q = q.where(DrumKit.title.ilike(f"%{filters.search}%"))
    if filters.is_free is not None:
        q = q.where(DrumKit.is_free == filters.is_free)
    if filters.tags:
        q = q.where(DrumKit.tags.overlap(filters.tags))

    count_q = select(func.count()).select_from(q.subquery())
    total = await db.scalar(count_q)

    q = (
        q.options(selectinload(DrumKit.samples))
        .order_by(DrumKit.created_at.desc())
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    result = await db.execute(q)
    return list(result.scalars().all()), total or 0


async def replace_sample_audio(
    db: AsyncSession,
    kit_id: uuid.UUID,
    sample_id: uuid.UUID,
    file: UploadFile,
) -> DrumSample:
    sample = await db.get(DrumSample, sample_id)
    if not sample or sample.drum_kit_id != kit_id:
        raise NotFoundError(f"Sample {sample_id} not found in kit {kit_id}")

    wav_bytes = await validate_wav_upload(file)

    if sample.file_s3_key:
        await s3_service.delete_object(sample.file_s3_key)

    raw_key = s3_service.s3_key_for_raw_drum_sample(str(sample_id))
    await s3_service.upload_bytes(raw_key, wav_bytes, "audio/wav")

    sample.file_s3_key = None
    sample.status = "processing"
    await db.commit()
    await db.refresh(sample)
    return sample


async def delete_drum_kit(db: AsyncSession, kit_id: uuid.UUID) -> None:
    result = await db.execute(
        select(DrumKit)
        .options(selectinload(DrumKit.samples))
        .where(DrumKit.id == kit_id)
    )
    kit = result.scalar_one_or_none()
    if not kit:
        raise NotFoundError(f"Drum kit {kit_id} not found")

    for sample in kit.samples:
        if sample.file_s3_key:
            await s3_service.delete_object(sample.file_s3_key)
        if sample.preview_s3_key:
            await s3_service.delete_object(sample.preview_s3_key)

    if kit.thumbnail_s3_key:
        await s3_service.delete_object(kit.thumbnail_s3_key)

    await db.delete(kit)
    await db.commit()
