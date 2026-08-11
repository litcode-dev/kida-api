import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from fastapi import UploadFile

from app.models.drone_pad import Drone, DronePad, DronePadCategory, MusicalKey
from app.models.purchase import Purchase
from app.models.monthly_download_usage import MonthlyQuotaType
from app.models.user import User
from app.schemas.drone_pad import (
    DronePadCategoryCreate,
    DronePadCreate,
    DronePadFilter,
    DronePadUpdate,
)
from app.exceptions import NotFoundError, EntitlementError
from app.services import monthly_quota_service, price_sync_service, s3_service
from app.utils.audio_validator import validate_wav_upload


async def create_category(
    db: AsyncSession,
    data: DronePadCategoryCreate,
    created_by: uuid.UUID,
) -> DronePadCategory:
    existing = await db.scalar(select(DronePadCategory).where(DronePadCategory.name == data.name))
    if existing:
        from app.exceptions import AppError
        raise AppError(f"Category '{data.name}' already exists", status_code=409)
    category = DronePadCategory(name=data.name, description=data.description, created_by=created_by)
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return category


async def get_category(db: AsyncSession, category_id: uuid.UUID) -> DronePadCategory:
    category = await db.get(DronePadCategory, category_id)
    if not category:
        raise NotFoundError(f"Drone pad category {category_id} not found")
    return category


async def list_categories(db: AsyncSession) -> list[DronePadCategory]:
    result = await db.scalars(select(DronePadCategory).order_by(DronePadCategory.name))
    return list(result.all())


async def delete_category(db: AsyncSession, category_id: uuid.UUID) -> None:
    category = await get_category(db, category_id)
    await db.execute(
        Drone.__table__.update()
        .where(Drone.category_id == category_id)
        .values(category_id=None)
    )
    await db.delete(category)
    await db.commit()


def _thumbnail_url_for_key(key: str | None) -> str | None:
    if not key:
        return None
    from app.config import get_settings
    base = get_settings().s3_cloudfront_url.rstrip("/")
    return f"{base}/{key}" if base else key


async def _load_drone(db: AsyncSession, drone_id: uuid.UUID) -> Drone | None:
    return await db.scalar(
        select(Drone)
        .options(
            selectinload(Drone.category),
            selectinload(Drone.pads).selectinload(DronePad.drone),
        )
        .where(Drone.id == drone_id)
    )


async def create_drone(
    db: AsyncSession,
    file: UploadFile,
    data: DronePadCreate,
    created_by: uuid.UUID,
    thumbnail: UploadFile | None = None,
) -> Drone:
    if data.category_id is not None:
        await get_category(db, data.category_id)
    wav_bytes = await validate_wav_upload(file)

    drone_id = str(uuid.uuid4())
    pad_id = str(uuid.uuid4())
    raw_key = s3_service.s3_key_for_raw_drone(pad_id)
    await s3_service.upload_bytes(raw_key, wav_bytes, "audio/wav")

    thumb_url = None
    thumb_key = None
    if thumbnail:
        thumb_bytes = await thumbnail.read()
        content_type = thumbnail.content_type or "image/jpeg"
        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
        thumb_key = s3_service.s3_key_for_drone_thumbnail(drone_id, ext)
        await s3_service.upload_bytes(thumb_key, thumb_bytes, content_type)
        thumb_url = _thumbnail_url_for_key(thumb_key)

    drone = Drone(
        id=uuid.UUID(drone_id),
        title=data.title,
        description=data.description,
        thumbnail_url=thumb_url,
        price=data.price,
        is_free=data.is_free,
        desired_price_usd=data.desired_price_usd,
        category_id=data.category_id,
        created_by=created_by,
    )
    if not drone.is_free:
        price_sync_service.ensure_sku(drone, "drone")
    pad = DronePad(
        id=uuid.UUID(pad_id),
        drone_id=drone.id,
        key=data.key,
        duration=0,
        thumbnail_s3_key=thumb_key,
        status="processing",
    )
    db.add(drone)
    db.add(pad)
    await db.commit()
    loaded = await get_drone(db, drone.id)
    return loaded


async def get_drone(db: AsyncSession, drone_id: uuid.UUID) -> Drone:
    drone = await _load_drone(db, drone_id)
    if not drone:
        raise NotFoundError(f"Drone {drone_id} not found")
    return drone


async def get_drone_pad(db: AsyncSession, pad_id: uuid.UUID) -> DronePad:
    pad = await db.scalar(
        select(DronePad)
        .options(selectinload(DronePad.drone).selectinload(Drone.category))
        .where(DronePad.id == pad_id)
    )
    if not pad:
        raise NotFoundError(f"Drone pad {pad_id} not found")
    return pad


async def list_drones(db: AsyncSession, filters: DronePadFilter) -> tuple[list[Drone], int]:
    q = select(Drone)
    if filters.created_by:
        q = q.where(Drone.created_by == filters.created_by)
    if filters.is_free is not None:
        q = q.where(Drone.is_free == filters.is_free)
    if filters.category_id is not None:
        q = q.where(Drone.category_id == filters.category_id)
    if filters.key:
        q = q.where(Drone.id.in_(select(DronePad.drone_id).where(DronePad.key == filters.key)))

    total = await db.scalar(select(func.count()).select_from(q.subquery()))

    q = (
        q.options(
            selectinload(Drone.category),
            selectinload(Drone.pads).selectinload(DronePad.drone),
        )
        .order_by(Drone.created_at.desc())
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    result = await db.scalars(q)
    return list(result.all()), total or 0


async def get_drones_by_ids(db: AsyncSession, drone_ids: list[uuid.UUID]) -> list[DronePad]:
    result = await db.scalars(
        select(DronePad)
        .options(selectinload(DronePad.drone))
        .where(DronePad.id.in_(drone_ids))
        .order_by(DronePad.key)
    )
    return list(result.all())


async def check_download_entitlement(db: AsyncSession, user: User, pad: DronePad) -> None:
    if pad.drone.is_free:
        return
    purchase = await db.scalar(
        select(Purchase).where(
            Purchase.user_id == user.id,
            (
                (Purchase.drone_pad_id == pad.id)
                | (Purchase.item_id == pad.drone_id)
                | (Purchase.item_id == pad.id)
            ),
        )
    )
    if not purchase:
        raise EntitlementError()


async def bulk_create_drones(
    db: AsyncSession,
    files: list[UploadFile],
    keys: list,
    title: str,
    price,
    is_free: bool,
    category_id: uuid.UUID | None,
    created_by: uuid.UUID,
    thumbnail: UploadFile | None = None,
    description: str | None = None,
) -> tuple[Drone, list[DronePad]]:
    if len(files) != len(keys):
        from app.exceptions import AppError
        raise AppError("Number of files must match number of keys", status_code=422)

    if category_id is not None:
        await get_category(db, category_id)

    drone_id = str(uuid.uuid4())
    thumb_url = None
    thumb_key = None
    if thumbnail:
        thumb_bytes = await thumbnail.read()
        content_type = thumbnail.content_type or "image/jpeg"
        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
        thumb_key = s3_service.s3_key_for_drone_thumbnail(drone_id, ext)
        await s3_service.upload_bytes(thumb_key, thumb_bytes, content_type)
        thumb_url = _thumbnail_url_for_key(thumb_key)

    drone = Drone(
        id=uuid.UUID(drone_id),
        title=title,
        description=description,
        thumbnail_url=thumb_url,
        price=price,
        is_free=is_free,
        category_id=category_id,
        created_by=created_by,
    )
    if not drone.is_free:
        price_sync_service.ensure_sku(drone, "drone")
    db.add(drone)
    await db.flush()

    pads = []
    for file, key in zip(files, keys):
        wav_bytes = await validate_wav_upload(file)
        pad_id = str(uuid.uuid4())
        raw_key = s3_service.s3_key_for_raw_drone(pad_id)
        await s3_service.upload_bytes(raw_key, wav_bytes, "audio/wav")
        del wav_bytes

        pad = DronePad(
            id=uuid.UUID(pad_id),
            drone_id=drone.id,
            key=key,
            duration=0,
            thumbnail_s3_key=thumb_key,
            status="processing",
        )
        db.add(pad)
        pads.append(pad)

    await db.commit()
    loaded = await get_drone(db, drone.id)
    return loaded, loaded.pads


async def _download_items_for_pads(
    db: AsyncSession, user: User, pads: list[DronePad]
) -> list[dict]:
    results = []
    for pad in pads:
        if pad.status != "ready":
            continue
        try:
            await check_download_entitlement(db, user, pad)
        except EntitlementError:
            continue
        if not pad.file_s3_key:
            continue
        download_url = await s3_service.get_download_url(pad.file_s3_key, expiry_seconds=900)
        pad.drone.download_count += 1
        results.append({
            "drone_id": str(pad.drone_id),
            "drone_pad_id": str(pad.id),
            "title": pad.drone.title,
            "key": pad.key,
            "signed_url": download_url,
            "aes_key": pad.aes_key,
            "aes_iv": pad.aes_iv,
            "expires_in_seconds": 900,
        })
    await db.commit()
    return results


def _pads_for_key(drone: Drone, key: MusicalKey | None) -> list[DronePad]:
    if key is None:
        return list(drone.pads)
    return [p for p in drone.pads if p.key == key]


async def get_drone_downloads(
    db: AsyncSession, user: User, drone_id: uuid.UUID, key: MusicalKey | None = None
) -> list[dict]:
    from app.services import free_tier_service

    drone = await get_drone(db, drone_id)
    pads = _pads_for_key(drone, key)
    if key is not None and not pads:
        raise NotFoundError(f"Drone {drone_id} has no pad in key {key.value}")
    await free_tier_service.enforce_drone_download(db, user, drone, key)
    # One slot per drone group, however many of its pads are taken.
    await monthly_quota_service.enforce(
        db, user, MonthlyQuotaType.drone, str(drone.id)
    )
    return await _download_items_for_pads(db, user, pads)


async def get_title_downloads(
    db: AsyncSession, user: User, title: str, key: MusicalKey | None = None
) -> list[dict]:
    from app.exceptions import FreeTierLimitError, MonthlyDownloadLimitError
    from app.services import free_tier_service

    drones = list(
        await db.scalars(
            select(Drone)
            .options(
                selectinload(Drone.category),
                selectinload(Drone.pads).selectinload(DronePad.drone),
            )
            .where(Drone.title.ilike(title))
            .order_by(Drone.created_at)
        )
    )
    if not drones:
        raise NotFoundError(f"No drones found with title '{title}'")

    items: list[dict] = []
    matched_any = False
    refusal: FreeTierLimitError | MonthlyDownloadLimitError | None = None
    for drone in drones:
        pads = _pads_for_key(drone, key)
        if not pads:
            continue
        matched_any = True
        try:
            await free_tier_service.enforce_drone_download(db, user, drone, key)
            await monthly_quota_service.enforce(
                db, user, MonthlyQuotaType.drone, str(drone.id)
            )
        except (FreeTierLimitError, MonthlyDownloadLimitError) as exc:
            # Serve whatever the allowance still covers; only refuse outright
            # when nothing could be served at all.
            refusal = exc
            continue
        items.extend(await _download_items_for_pads(db, user, pads))
    if key is not None and not matched_any:
        raise NotFoundError(f"No '{title}' pad in key {key.value}")
    if not items and refusal is not None:
        raise refusal
    return items


async def update_drone(
    db: AsyncSession,
    drone_id: uuid.UUID,
    data: DronePadUpdate,
    thumbnail: UploadFile | None = None,
) -> Drone:
    drone = await get_drone(db, drone_id)

    if thumbnail:
        old_thumb_key = drone.pads[0].thumbnail_s3_key if drone.pads else None
        if old_thumb_key:
            await s3_service.delete_object(old_thumb_key)
        thumb_bytes = await thumbnail.read()
        content_type = thumbnail.content_type or "image/jpeg"
        ext = content_type.split("/")[-1] if "/" in content_type else "jpg"
        new_thumb_key = s3_service.s3_key_for_drone_thumbnail(str(drone_id), ext)
        await s3_service.upload_bytes(new_thumb_key, thumb_bytes, content_type)
        drone.thumbnail_url = _thumbnail_url_for_key(new_thumb_key)
        for pad in drone.pads:
            pad.thumbnail_s3_key = new_thumb_key

    update_fields = data.model_dump(exclude_none=True)
    new_desired = update_fields.pop("desired_price_usd", None)
    for field, value in update_fields.items():
        setattr(drone, field, value)

    if new_desired is not None and new_desired != drone.desired_price_usd:
        drone.desired_price_usd = new_desired
        price_sync_service.mark_price_dirty(drone)
        if not drone.is_free:
            price_sync_service.ensure_sku(drone, "drone")

    await db.commit()
    return await get_drone(db, drone_id)


async def replace_pad_audio(
    db: AsyncSession,
    drone_id: uuid.UUID,
    pad_id: uuid.UUID,
    file: UploadFile,
) -> DronePad:
    pad = await get_drone_pad(db, pad_id)
    if pad.drone_id != drone_id:
        raise NotFoundError(f"Pad {pad_id} not found in drone {drone_id}")

    wav_bytes = await validate_wav_upload(file)

    if pad.file_s3_key:
        await s3_service.delete_object(pad.file_s3_key)
    if pad.preview_s3_key:
        await s3_service.delete_object(pad.preview_s3_key)

    raw_key = s3_service.s3_key_for_raw_drone(str(pad_id))
    await s3_service.upload_bytes(raw_key, wav_bytes, "audio/wav")

    pad.file_s3_key = None
    pad.preview_s3_key = None
    pad.status = "processing"
    await db.commit()
    await db.refresh(pad)
    return pad


async def delete_drone(db: AsyncSession, drone_id: uuid.UUID) -> None:
    drone = await get_drone(db, drone_id)
    for pad in drone.pads:
        if pad.file_s3_key:
            await s3_service.delete_object(pad.file_s3_key)
        if pad.preview_s3_key:
            await s3_service.delete_object(pad.preview_s3_key)
        if pad.thumbnail_s3_key:
            await s3_service.delete_object(pad.thumbnail_s3_key)
    await db.delete(drone)
    await db.commit()
