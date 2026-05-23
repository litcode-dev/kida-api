# app/routers/producer.py
import uuid
from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.exceptions import AppError, ForbiddenError, NotFoundError
from app.middleware.auth_middleware import require_producer
from app.middleware.rate_limit import limiter
from app.schemas.common import success
from app.schemas.drum_kit import DrumKitCreate, DrumKitFilter, DrumKitResponse, DrumKitUpdate
from app.schemas.drone_pad import (
    DronePadCategoryCreate,
    DronePadCategoryResponse,
    DronePadCreate,
    DronePadUpdate,
    DroneResponse,
)
from app.schemas.loop import LoopCreate, LoopFilter, LoopUpdate, LoopResponse
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


def _assert_owns(resource, producer, label: str) -> None:
    if resource.created_by != producer.id:
        raise ForbiddenError(f"You do not own this {label}")


@router.get("/analytics")
@limiter.limit("60/minute")
async def producer_analytics(
    request: Request,
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

@router.get("/loops")
@limiter.limit("60/minute")
async def list_producer_loops(
    request: Request,
    search: str | None = None,
    genre: Genre | None = None,
    bpm_min: int | None = None,
    bpm_max: int | None = None,
    key: str | None = None,
    tempo_feel: TempoFeel | None = None,
    is_free: bool | None = None,
    sort: str = "newest",
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    filters = LoopFilter(
        search=search, genre=genre, bpm_min=bpm_min, bpm_max=bpm_max,
        key=key, tempo_feel=tempo_feel, is_free=is_free, sort=sort,
        page=page, page_size=page_size, created_by=producer.id,
    )
    loops, total = await loop_service.list_loops(db, filters)
    return success({
        "items": [LoopResponse.model_validate(l).model_dump() for l in loops],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.post("/loops")
@limiter.limit("10/minute")
async def upload_loop(
    request: Request,
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
@limiter.limit("60/minute")
async def loop_upload_status(
    request: Request,
    loop_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    loop = await loop_service.get_loop(db, loop_id)
    _assert_owns(loop, producer, "loop")
    return success({"id": str(loop.id), "status": loop.status})


@router.put("/loops/{loop_id}")
@limiter.limit("20/minute")
async def update_loop(
    request: Request,
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
    existing = await loop_service.get_loop(db, loop_id)
    _assert_owns(existing, producer, "loop")
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

@router.get("/stem-packs")
@limiter.limit("60/minute")
async def list_producer_stem_packs(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    packs, total = await stem_pack_service.list_stem_packs(db, producer.id, page, page_size)
    return success({
        "items": [StemPackResponse.model_validate(p).model_dump() for p in packs],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.post("/stem-packs")
@limiter.limit("30/minute")
async def create_stem_pack(
    request: Request,
    body: StemPackCreate,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    pack = await stem_pack_service.create_stem_pack(db, body, producer.id)
    return success(StemPackResponse.model_validate(pack).model_dump(), "StemPack created")


@router.post("/stem-packs/{pack_id}/stems")
@limiter.limit("10/minute")
async def add_stem(
    request: Request,
    pack_id: uuid.UUID,
    file: UploadFile = File(...),
    label: str = Form(...),
    duration: int = Form(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    from app.models.stem_pack import StemPack
    pack = await db.get(StemPack, pack_id)
    if not pack:
        raise NotFoundError("StemPack not found")
    _assert_owns(pack, producer, "stem pack")
    data = StemCreate(label=label, duration=duration)
    stem = await stem_pack_service.add_stem_to_pack(db, pack_id, file, data)
    return success(StemResponse.model_validate(stem).model_dump(), "Stem added")


@router.put("/stem-packs/{pack_id}")
@limiter.limit("20/minute")
async def update_stem_pack(
    request: Request,
    pack_id: uuid.UUID,
    body: StemPackCreate,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    from app.models.stem_pack import StemPack
    pack = await db.get(StemPack, pack_id)
    if not pack:
        raise NotFoundError("StemPack not found")
    _assert_owns(pack, producer, "stem pack")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(pack, field, value)
    await db.commit()
    await db.refresh(pack)
    return success(StemPackResponse.model_validate(pack).model_dump(), "StemPack updated")


# --- Drone pad endpoints ---

@router.get("/drones")
@limiter.limit("60/minute")
async def list_producer_drones(
    request: Request,
    key: MusicalKey | None = None,
    is_free: bool | None = None,
    category_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    from app.schemas.drone_pad import DronePadFilter
    filters = DronePadFilter(
        key=key, is_free=is_free, category_id=category_id,
        page=page, page_size=page_size, created_by=producer.id,
    )
    drones, total = await drone_service.list_drones(db, filters)
    return success({
        "items": [DroneResponse.model_validate(d).model_dump(mode="json") for d in drones],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.post("/drones/categories")
@limiter.limit("30/minute")
async def create_drone_category(
    request: Request,
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
@limiter.limit("60/minute")
async def list_drone_categories(
    request: Request,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    categories = await drone_service.list_categories(db)
    return success([DronePadCategoryResponse.model_validate(c).model_dump() for c in categories])


@router.post("/drones")
@limiter.limit("10/minute")
async def upload_drone(
    request: Request,
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
@limiter.limit("5/minute")
async def bulk_upload_drones(
    request: Request,
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
@limiter.limit("60/minute")
async def bulk_drone_upload_status(
    request: Request,
    ids: str,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    parsed_ids = [i.strip() for i in ids.split(",") if i.strip()]
    try:
        validated_ids = [uuid.UUID(i) for i in parsed_ids]
    except ValueError:
        raise AppError("Invalid UUID in ids", status_code=422)

    pads = await drone_service.get_drones_by_ids(db, validated_ids)
    owned = [p for p in pads if p.drone.created_by == producer.id]
    return success([
        {"id": str(p.id), "drone_id": str(p.drone_id), "key": p.key, "status": p.status}
        for p in owned
    ])


@router.get("/drones/{drone_id}/status")
@limiter.limit("60/minute")
async def drone_upload_status(
    request: Request,
    drone_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    drone = await drone_service.get_drone(db, drone_id)
    _assert_owns(drone, producer, "drone")
    return success({
        "id": str(drone.id),
        "status": "ready" if all(p.status == "ready" for p in drone.pads) else "processing",
        "pads": [{"id": str(p.id), "key": p.key, "status": p.status} for p in drone.pads],
    })


@router.put("/drones/{drone_id}")
@limiter.limit("20/minute")
async def update_drone(
    request: Request,
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
    existing = await drone_service.get_drone(db, drone_id)
    _assert_owns(existing, producer, "drone")
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
@limiter.limit("20/minute")
async def replace_drone_pad_audio(
    request: Request,
    drone_id: uuid.UUID,
    pad_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    existing = await drone_service.get_drone(db, drone_id)
    _assert_owns(existing, producer, "drone")
    pad = await drone_service.replace_pad_audio(db, drone_id, pad_id, file)
    import structlog as _structlog
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="replace_drone_pad_audio", error=str(e))
    process_drone_upload.delay(str(pad_id))
    return success({"pad_id": str(pad.id), "status": pad.status}, "Pad audio replacement queued")


# --- Drum kit endpoints ---

@router.post(
    "/drum-kits",
    summary="Create drum kit",
    description=(
        "Creates a drum kit and uploads all samples in one request. "
        "For paid kits (`is_free=false`), `price` is required — `store_product_id` is auto-generated. "
        "Samples are queued for background processing after upload; their `status` starts as `processing`."
    ),
    response_description="Created drum kit with samples",
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Producer or admin role required"},
        422: {"description": "Validation error — missing price for paid kit, or sample/label count mismatch"},
    },
    status_code=201,
)
@limiter.limit("10/minute")
async def create_drum_kit(
    request: Request,
    thumbnail: UploadFile | None = File(None),
    title: str = Form(...),
    description: str | None = Form(None),
    tags: str = Form(""),
    is_free: bool = Form(True),
    price: Decimal | None = Form(None),
    sample_files: list[UploadFile] = File(...),
    sample_labels: str = Form(...),  # comma-separated labels matching sample_files order
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    import structlog as _structlog
    labels = [l.strip() for l in sample_labels.split(",") if l.strip()]
    data = DrumKitCreate(
        title=title,
        description=description,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        is_free=is_free,
        price=price,
    )
    kit, sample_ids = await drum_kit_service.create_drum_kit(
        db, data, producer.id, sample_files, labels, thumbnail=thumbnail
    )
    for sid in sample_ids:
        process_drum_sample_upload.delay(sid)
    send_new_content_emails.delay(kit.title, "drum_kit")
    try:
        await cache_service.delete_pattern("drum_kit:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="create_drum_kit", error=str(e))
    return success(DrumKitResponse.model_validate(kit).model_dump(), "Drum kit created, samples queued for processing")


@router.get(
    "/drum-kits",
    summary="List drum kits (producer)",
    description="Paginated list of all drum kits with samples. Supports the same filters as the public endpoint.",
    response_description="Paginated drum kit list",
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Producer or admin role required"},
    },
)
@limiter.limit("60/minute")
async def list_drum_kits_producer(
    request: Request,
    search: str | None = None,
    is_free: bool | None = None,
    tags: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    import asyncio as _asyncio
    from app.routers.drum_kits import _kit_to_dict
    filters = DrumKitFilter(
        search=search,
        is_free=is_free,
        tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None,
        page=page,
        page_size=page_size,
        created_by=producer.id,
    )
    kits, total = await drum_kit_service.list_drum_kits(db, filters)
    return success({
        "items": list(await _asyncio.gather(*[_kit_to_dict(k) for k in kits])),
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.put("/drum-kits/{kit_id}")
@limiter.limit("20/minute")
async def update_drum_kit(
    request: Request,
    kit_id: uuid.UUID,
    thumbnail: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    existing = await drum_kit_service.get_drum_kit(db, kit_id)
    _assert_owns(existing, producer, "drum kit")
    data = DrumKitUpdate(title=title, description=description, price=price, is_free=is_free)
    kit = await drum_kit_service.update_drum_kit(db, kit_id, data, thumbnail=thumbnail)
    return success(DrumKitResponse.model_validate(kit).model_dump(), "Drum kit updated")


@router.patch("/drum-kits/{kit_id}/samples/{sample_id}")
@limiter.limit("20/minute")
async def replace_drum_sample_audio(
    request: Request,
    kit_id: uuid.UUID,
    sample_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    existing = await drum_kit_service.get_drum_kit(db, kit_id)
    _assert_owns(existing, producer, "drum kit")
    sample = await drum_kit_service.replace_sample_audio(db, kit_id, sample_id, file)
    process_drum_sample_upload.delay(str(sample_id))
    return success({"sample_id": str(sample.id), "status": sample.status}, "Sample audio replacement queued")
