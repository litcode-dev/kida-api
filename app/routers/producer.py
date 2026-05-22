# app/routers/producer.py
import uuid
from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import ValidationError
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.exceptions import AppError, NotFoundError
from app.middleware.auth_middleware import require_producer
from app.schemas.common import success
from app.schemas.drum_kit import DrumKitCreate, DrumKitFilter, DrumKitResponse, DrumKitUpdate
from app.schemas.drone_pad import (
    DronePadCategoryCreate,
    DronePadCategoryResponse,
    DronePadCreate,
    DronePadUpdate,
    DroneResponse,
)
from app.schemas.loop import LoopCreate, LoopUpdate, LoopResponse
from app.schemas.producer_analytics import AnalyticsParams, AnalyticsPeriod
from app.schemas.stem_pack import StemPackCreate, StemCreate, StemPackResponse, StemResponse
from app.models.drum_kit import DrumKit
from app.models.drone_pad import MusicalKey
from app.models.loop import Genre, TempoFeel
from app.models.user import User
from app.services import cache_service, drum_kit_service, drone_service, loop_service, stem_pack_service
from app.services.producer_analytics_service import get_producer_analytics
from app.tasks.notification_tasks import send_new_content_emails
from app.tasks.upload_tasks import process_drone_upload, process_drum_sample_upload, process_loop_upload

router = APIRouter(prefix="/producer", tags=["producer"])


@router.get("/analytics")
async def producer_analytics(
    period: AnalyticsPeriod = Query(AnalyticsPeriod.all),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    loops_page: int = Query(1),
    drones_page: int = Query(1),
    drum_kits_page: int = Query(1),
    page_size: int = Query(20),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    try:
        params = AnalyticsParams(
            period=period,
            from_date=from_date,
            to_date=to_date,
            loops_page=loops_page,
            drones_page=drones_page,
            drum_kits_page=drum_kits_page,
            page_size=page_size,
        )
    except ValidationError as e:
        err = e.errors()[0]
        msg = str(err.get("ctx", {}).get("error", err["msg"]))
        raise AppError(msg, status_code=422)

    data = await get_producer_analytics(db, producer.id, params)
    return success(data)


# --- Loop endpoints ---

@router.post("/loops")
async def upload_loop(
    file: UploadFile = File(...),
    thumbnail: UploadFile | None = File(None),
    title: str = Form(...),
    genre: Genre = Form(...),
    bpm: int = Form(...),
    tempo_feel: TempoFeel = Form(...),
    price: Decimal = Form(...),
    is_free: bool = Form(False),
    tags: str = Form(""),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    data = LoopCreate(
        title=title, genre=genre, bpm=bpm,
        tempo_feel=tempo_feel, price=price, is_free=is_free,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    loop = await loop_service.create_loop(db, file, data, producer.id, thumbnail=thumbnail)
    process_loop_upload.delay(str(loop.id))
    send_new_content_emails.delay(loop.title, "loop")
    return success(LoopResponse.model_validate(loop).model_dump(), "Loop upload queued")


@router.get("/loops/{loop_id}/status")
async def loop_upload_status(
    loop_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    loop = await loop_service.get_loop(db, loop_id)
    return success({"id": str(loop.id), "status": loop.status})


@router.put("/loops/{loop_id}")
async def update_loop(
    loop_id: uuid.UUID,
    thumbnail: UploadFile | None = File(None),
    file: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    genre: Genre | None = Form(None),
    bpm: int | None = Form(None),
    tempo_feel: TempoFeel | None = Form(None),
    tags: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    data = LoopUpdate(
        title=title,
        description=description,
        genre=genre,
        bpm=bpm,
        tempo_feel=tempo_feel,
        tags=tags_list,
        price=price,
        is_free=is_free,
    )
    loop, should_reprocess = await loop_service.update_loop(
        db, loop_id, data, thumbnail=thumbnail, file=file
    )
    if should_reprocess:
        process_loop_upload.delay(str(loop_id))
    return success(LoopResponse.model_validate(loop).model_dump(), "Loop updated")


# --- StemPack endpoints ---

@router.post("/stem-packs")
async def create_stem_pack(
    body: StemPackCreate,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    pack = await stem_pack_service.create_stem_pack(db, body, producer.id)
    return success(StemPackResponse.model_validate(pack).model_dump(), "StemPack created")


@router.post("/stem-packs/{pack_id}/stems")
async def add_stem(
    pack_id: uuid.UUID,
    file: UploadFile = File(...),
    label: str = Form(...),
    duration: int = Form(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    data = StemCreate(label=label, duration=duration)
    stem = await stem_pack_service.add_stem_to_pack(db, pack_id, file, data)
    return success(StemResponse.model_validate(stem).model_dump(), "Stem added")


@router.put("/stem-packs/{pack_id}")
async def update_stem_pack(
    pack_id: uuid.UUID,
    body: StemPackCreate,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    from app.models.stem_pack import StemPack
    pack = await db.get(StemPack, pack_id)
    if not pack:
        raise NotFoundError("StemPack not found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(pack, field, value)
    await db.commit()
    await db.refresh(pack)
    return success(StemPackResponse.model_validate(pack).model_dump(), "StemPack updated")


# --- Drone pad endpoints ---

@router.post("/drones/categories")
async def create_drone_category(
    body: DronePadCategoryCreate,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    import structlog as _structlog
    category = await drone_service.create_category(db, body, producer.id)
    data = DronePadCategoryResponse.model_validate(category).model_dump(mode="json")
    try:
        await cache_service.delete("drone:categories")
        await cache_service.set(f"drone:category:{category.id}", data, cache_service.TTL_DRONE_CATEGORIES)
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="create_drone_category", error=str(e))
    return success(data, "Category created")


@router.get("/drones/categories")
async def list_drone_categories(
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    categories = await drone_service.list_categories(db)
    return success([DronePadCategoryResponse.model_validate(c).model_dump() for c in categories])


@router.post("/drones")
async def upload_drone(
    file: UploadFile = File(...),
    thumbnail: UploadFile | None = File(None),
    title: str = Form(...),
    description: str | None = Form(None),
    key: MusicalKey = Form(...),
    price: Decimal | None = Form(None),
    is_free: bool = Form(False),
    category_id: uuid.UUID | None = Form(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    from app.exceptions import AppError
    if not is_free and price is None:
        raise AppError("price is required for paid drone pads", status_code=422)
    data = DronePadCreate(
        title=title,
        description=description,
        key=key,
        price=price,
        is_free=is_free,
        category_id=category_id,
    )
    drone = await drone_service.create_drone(db, file, data, producer.id, thumbnail=thumbnail)
    import structlog as _structlog
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="upload_drone", error=str(e))
    for pad in drone.pads:
        process_drone_upload.delay(str(pad.id))
    send_new_content_emails.delay(drone.name, "drone_pad")
    return success(DroneResponse.model_validate(drone).model_dump(), "Drone pad upload queued")


@router.post("/drones/bulk")
async def bulk_upload_drones(
    files: list[UploadFile] = File(...),
    keys: str = Form(...),
    title: str = Form(...),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool = Form(False),
    category_id: uuid.UUID | None = Form(None),
    thumbnail: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    from app.exceptions import AppError

    if not is_free and price is None:
        raise AppError("price is required for paid drone pads", status_code=422)

    parsed_keys = [k.strip() for k in keys.split(",") if k.strip()]
    try:
        validated_keys = [MusicalKey(k) for k in parsed_keys]
    except ValueError as e:
        raise AppError(f"Invalid key value: {e}", status_code=422)

    if len(files) != len(validated_keys):
        raise AppError(
            f"Got {len(files)} file(s) but {len(validated_keys)} key(s); counts must match",
            status_code=422,
        )

    drone, pads = await drone_service.bulk_create_drones(
        db, files, validated_keys, title, price, is_free, category_id, producer.id,
        thumbnail=thumbnail, description=description
    )
    import structlog as _structlog
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="bulk_upload_drones", error=str(e))
    for pad in pads:
        process_drone_upload.delay(str(pad.id))
    send_new_content_emails.delay(drone.name, "drone_pad")

    return success(
        DroneResponse.model_validate(drone).model_dump(),
        f"{len(pads)} drone pad(s) upload queued",
    )


@router.get("/drones/bulk/status")
async def bulk_drone_upload_status(
    ids: str,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    from app.exceptions import AppError
    parsed_ids = [i.strip() for i in ids.split(",") if i.strip()]
    try:
        validated_ids = [uuid.UUID(i) for i in parsed_ids]
    except ValueError:
        raise AppError("Invalid UUID in ids", status_code=422)

    drones = await drone_service.get_drones_by_ids(db, validated_ids)
    return success([
        {"id": str(d.id), "drone_id": str(d.drone_id), "key": d.key, "status": d.status}
        for d in drones
    ])


@router.get("/drones/{drone_id}/status")
async def drone_upload_status(
    drone_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    drone = await drone_service.get_drone(db, drone_id)
    return success({
        "id": str(drone.id),
        "status": "ready" if all(p.status == "ready" for p in drone.pads) else "processing",
        "pads": [{"id": str(p.id), "key": p.key, "status": p.status} for p in drone.pads],
    })


@router.put("/drones/{drone_id}")
async def update_drone(
    drone_id: uuid.UUID,
    thumbnail: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    category_id: uuid.UUID | None = Form(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    data = DronePadUpdate(
        title=title,
        description=description,
        price=price,
        is_free=is_free,
        category_id=category_id,
    )
    drone = await drone_service.update_drone(db, drone_id, data, thumbnail=thumbnail)
    import structlog as _structlog
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="update_drone", error=str(e))
    return success(DroneResponse.model_validate(drone).model_dump(), "Drone updated")


@router.patch("/drones/{drone_id}/pads/{pad_id}")
async def replace_drone_pad_audio(
    drone_id: uuid.UUID,
    pad_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    pad = await drone_service.replace_pad_audio(db, drone_id, pad_id, file)
    import structlog as _structlog
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="replace_drone_pad_audio", error=str(e))
    process_drone_upload.delay(str(pad_id))
    return success({"pad_id": str(pad.id), "status": pad.status}, "Pad audio replacement queued")
