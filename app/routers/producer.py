# app/routers/producer.py
import uuid
from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.exceptions import AppError, ForbiddenError, parse_model
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
from app.routers.stem_pack_management import build_stem_pack_router
from app.models.drone_pad import MusicalKey
from app.models.loop import Genre, TempoFeel
from app.services import cache_service, drum_kit_service, drone_service, loop_service
from app.services.producer_analytics_service import get_producer_analytics
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
    params = parse_model(
        AnalyticsParams,
        period=period,
        from_date=from_date,
        to_date=to_date,
        loops_page=loops_page,
        drones_page=drones_page,
        drum_kits_page=drum_kits_page,
        page_size=page_size,
    )

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
    time_signature: list[str] | None = Query(
        None,
        description=(
            "Time signatures, e.g. `6/8` or `6/8,9/8,12/8`. Repeating the "
            "parameter works too. A loop matches when it carries **any** of them."
        ),
    ),
    tempo_feel: TempoFeel | None = None,
    is_free: bool | None = None,
    tags: str | None = Query(
        None,
        description=(
            "Comma-separated tags, e.g. `808,dark`. A loop matches when it "
            "carries **any** of them."
        ),
    ),
    sort: str = "newest",
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    filters = parse_model(
        LoopFilter,
        search=search, genre=genre, bpm_min=bpm_min, bpm_max=bpm_max,
        key=key, time_signature=time_signature, tempo_feel=tempo_feel,
        is_free=is_free, sort=sort,
        tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None,
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
    title: str = Form(..., max_length=255),
    genre: Genre = Form(...),
    bpm: int = Form(...),
    time_signature: str = Form("4/4", max_length=16),
    tempo_feel: TempoFeel = Form(...),
    price: Decimal = Form(...),
    is_free: bool = Form(False),
    tags: str = Form(""),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    data = parse_model(
        LoopCreate,
        title=title, genre=genre, bpm=bpm,
        time_signature=time_signature, tempo_feel=tempo_feel,
        price=price, is_free=is_free,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    loop = await loop_service.create_loop(db, file, data, producer.id, thumbnail=thumbnail)
    process_loop_upload.delay(str(loop.id))
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
    title: str | None = Form(None, max_length=255),
    description: str | None = Form(None),
    genre: Genre | None = Form(None),
    bpm: int | None = Form(None),
    time_signature: str | None = Form(None, max_length=16),
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
    data = parse_model(
        LoopUpdate,
        title=title,
        description=description,
        genre=genre,
        bpm=bpm,
        time_signature=time_signature,
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
# Packs, song parts, stems and arrangements are mounted from the shared builder
# so the producer and admin surfaces cannot drift apart. Producers are held to
# packs they created.
router.include_router(build_stem_pack_router(require_producer, enforce_ownership=True))


# --- Drone pad endpoints ---

@router.get("/drones")
@limiter.limit("60/minute")
async def list_producer_drones(
    request: Request,
    search: str | None = None,
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
        search=search, key=key, is_free=is_free, category_id=category_id,
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
    title: str = Form(..., max_length=255),
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
    data = parse_model(
        DronePadCreate,
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
    return success(DroneResponse.model_validate(drone).model_dump(), "Drone pad upload queued")


@router.post("/drones/bulk")
@limiter.limit("5/minute")
async def bulk_upload_drones(
    request: Request,
    files: list[UploadFile] = File(...),
    keys: str = Form(...),
    title: str = Form(..., max_length=255),
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
    title: str | None = Form(None, max_length=255),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    category_id: uuid.UUID | None = Form(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    existing = await drone_service.get_drone(db, drone_id)
    _assert_owns(existing, producer, "drone")
    data = parse_model(
        DronePadUpdate,
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
    title: str = Form(..., max_length=255),
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
    data = parse_model(
        DrumKitCreate,
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
    title: str | None = Form(None, max_length=255),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    db: AsyncSession = Depends(get_db),
    producer=Depends(require_producer),
):
    existing = await drum_kit_service.get_drum_kit(db, kit_id)
    _assert_owns(existing, producer, "drum kit")
    data = parse_model(
        DrumKitUpdate,
        title=title, description=description, price=price, is_free=is_free,
    )
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
