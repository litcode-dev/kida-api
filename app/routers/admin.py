from fastapi import APIRouter, Depends, Query, UploadFile, File, Form
from pydantic import BaseModel, EmailStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from decimal import Decimal
from app.database import get_db
from app.middleware.auth_middleware import require_admin
from app.schemas.user import UserResponse
from app.schemas.common import success
from app.schemas.loop import LoopCreate, LoopUpdate, LoopResponse
from app.schemas.stem_pack import StemPackCreate, StemCreate, StemPackResponse, StemResponse
from app.schemas.drone_pad import (
    DronePadCategoryCreate,
    DronePadCategoryResponse,
    DronePadCreate,
    DronePadUpdate,
    DroneResponse,
)
from app.schemas.drum_kit import DrumKitCreate, DrumKitFilter, DrumKitResponse, DrumKitUpdate
from app.schemas.producer_analytics import AnalyticsPeriod, AnalyticsParams
from app.models.user import User, UserRole
from app.models.loop import Genre, TempoFeel
from app.models.drone_pad import MusicalKey
from app.exceptions import NotFoundError, AppError
from app.services import loop_service, stem_pack_service, drone_service, drum_kit_service, cache_service
from app.services.admin_analytics_service import get_platform_analytics
from app.tasks.notification_tasks import send_new_content_emails
from app.tasks.upload_tasks import process_drone_upload, process_drum_sample_upload, process_loop_upload
import uuid
from datetime import date

router = APIRouter(prefix="/admin", tags=["admin"])


class EmailTestRequest(BaseModel):
    email: EmailStr


@router.post(
    "/email/test",
    summary="Send a test email",
    description=(
        "Sends a test welcome email to the specified address using the configured email backend. "
        "With `EMAIL_BACKEND=fallback`, Resend is tried first and SMTP is used if Resend fails. "
        "Check worker logs for `email.sent` or `email.failed` to confirm delivery."
    ),
    response_description="Confirmation that the email was dispatched",
    responses={
        200: {"description": "Email dispatched (check logs for actual delivery status)"},
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
        422: {"description": "Invalid email address"},
    },
)
async def test_email(body: EmailTestRequest, _: User = Depends(require_admin)):
    from app.services.email_service import send_email, registration_html, registration_text
    await send_email(
        to=body.email,
        subject="Kida — Email test",
        html=registration_html("there"),
        text=registration_text("there"),
    )
    return success(message="Test email dispatched", data={"to": body.email})


# --- Loop endpoints ---

@router.delete("/loops/{loop_id}")
async def delete_loop(
    loop_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    await loop_service.delete_loop(db, loop_id)
    return success(message="Loop deleted")


# --- StemPack endpoints ---

@router.delete("/stem-packs/{pack_id}")
async def delete_stem_pack(
    pack_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    from app.models.stem_pack import StemPack, Stem
    from app.services import s3_service
    pack = await db.get(StemPack, pack_id)
    if not pack:
        raise NotFoundError("StemPack not found")
    stems = await db.scalars(select(Stem).where(Stem.stem_pack_id == pack_id))
    for stem in stems.all():
        if stem.file_s3_key:
            await s3_service.delete_object(stem.file_s3_key)
        await db.delete(stem)
    await db.delete(pack)
    await db.commit()
    return success(message="StemPack deleted")


# --- User management endpoints (admin only) ---

@router.get("/users")
async def list_users(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    offset = (page - 1) * page_size
    total = await db.scalar(select(func.count()).select_from(User))
    users = await db.scalars(select(User).offset(offset).limit(page_size))
    return success({
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [UserResponse.model_validate(u).model_dump() for u in users.all()],
    })


@router.put("/users/{user_id}/role")
async def change_user_role(
    user_id: uuid.UUID,
    role: UserRole,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")
    user.role = role
    await db.commit()
    await db.refresh(user)
    return success(UserResponse.model_validate(user).model_dump(), "Role updated")


# --- AI administration ---

@router.put("/users/{user_id}/ai-enabled")
async def toggle_user_ai(
    user_id: uuid.UUID,
    enabled: bool,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")
    user.ai_enabled = enabled
    await db.commit()
    return success(
        {"ai_enabled": user.ai_enabled},
        f"AI {'enabled' if enabled else 'disabled'} for user",
    )


@router.get("/ai/generations")
async def list_all_generations(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    from app.models.ai_generation import AIGeneration
    from app.schemas.ai_generation import AIGenerationResponse
    offset = (page - 1) * page_size
    total = await db.scalar(select(func.count()).select_from(AIGeneration))
    gens = await db.scalars(
        select(AIGeneration)
        .order_by(AIGeneration.created_at.desc())
        .offset(offset).limit(page_size)
    )
    return success({
        "items": [AIGenerationResponse.model_validate(g).model_dump() for g in gens.all()],
        "total": total or 0,
        "page": page,
        "page_size": page_size,
    })


# --- Drone pad administration ---

@router.delete("/drones/categories/{category_id}")
async def delete_drone_category(
    category_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    import structlog as _structlog
    await drone_service.delete_category(db, category_id)
    try:
        await cache_service.delete("drone:categories")
        await cache_service.delete(f"drone:category:{category_id}")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="delete_drone_category", error=str(e))
    return success(message="Category deleted")


@router.delete("/drones/{drone_id}")
async def delete_drone(
    drone_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    await drone_service.delete_drone(db, drone_id)
    import structlog as _structlog
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="delete_drone", error=str(e))
    return success(message="Drone pad deleted")



# --- Drum kit endpoints ---

@router.delete(
    "/drum-kits/{kit_id}",
    summary="Delete drum kit",
    description="Permanently deletes the drum kit and all associated S3 assets (samples, thumbnail). Irreversible.",
    response_description="Deletion confirmation",
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
        404: {"description": "Drum kit not found"},
    },
)
async def delete_drum_kit(
    kit_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    import structlog as _structlog
    await drum_kit_service.delete_drum_kit(db, kit_id)
    try:
        await cache_service.delete(f"drum_kit:detail:{kit_id}")
        await cache_service.delete_pattern("drum_kit:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="delete_drum_kit", error=str(e))
    return success(message="Drum kit deleted")



# --- Newsletter endpoints ---

@router.get(
    "/newsletter/subscribers",
    summary="List newsletter subscribers",
    description="Returns paginated newsletter subscribers. Filter by `active=true` for active only or `active=false` for unsubscribed.",
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
    },
)
async def list_newsletter_subscribers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    active: bool | None = Query(None, description="Filter by subscription status"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    from app.models.newsletter import NewsletterSubscriber

    query = select(NewsletterSubscriber)
    count_query = select(func.count()).select_from(NewsletterSubscriber)

    if active is not None:
        query = query.where(NewsletterSubscriber.is_active == active)
        count_query = count_query.where(NewsletterSubscriber.is_active == active)

    total = await db.scalar(count_query)
    subscribers = await db.scalars(
        query.order_by(NewsletterSubscriber.subscribed_at.desc())
             .offset((page - 1) * page_size)
             .limit(page_size)
    )

    data = [
        {
            "email": s.email,
            "is_active": s.is_active,
            "subscribed_at": s.subscribed_at.isoformat(),
            "unsubscribed_at": s.unsubscribed_at.isoformat() if s.unsubscribed_at else None,
        }
        for s in subscribers.all()
    ]

    return success({
        "subscribers": data,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": -(-total // page_size),
    })


# --- Platform analytics ---

@router.get(
    "/analytics",
    summary="Platform analytics (admin)",
    description=(
        "Returns platform-wide revenue, user growth, top-selling content, and top producers "
        "for the specified time window. Defaults to all-time if no period is given."
    ),
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
        422: {"description": "Invalid period or mismatched date range"},
    },
)
async def platform_analytics(
    period: AnalyticsPeriod = Query(AnalyticsPeriod.all),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    try:
        params = AnalyticsParams(period=period, from_date=from_date, to_date=to_date)
    except ValidationError as e:
        err = e.errors()[0]
        msg = str(err.get("ctx", {}).get("error", err["msg"]))
        raise AppError(msg, status_code=422)
    data = await get_platform_analytics(db, params)
    return success(data)


# --- Loop upload / management (admin mirror of producer endpoints) ---

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
    admin=Depends(require_admin),
):
    data = LoopCreate(
        title=title, genre=genre, bpm=bpm,
        tempo_feel=tempo_feel, price=price, is_free=is_free,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    loop = await loop_service.create_loop(db, file, data, admin.id, thumbnail=thumbnail)
    process_loop_upload.delay(str(loop.id))
    send_new_content_emails.delay(loop.title, "loop")
    return success(LoopResponse.model_validate(loop).model_dump(), "Loop upload queued")


@router.get("/loops/{loop_id}/status")
async def loop_upload_status(
    loop_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
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
    admin=Depends(require_admin),
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


# --- StemPack endpoints (admin mirror) ---

@router.post("/stem-packs")
async def create_stem_pack(
    body: StemPackCreate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    pack = await stem_pack_service.create_stem_pack(db, body, admin.id)
    return success(StemPackResponse.model_validate(pack).model_dump(), "StemPack created")


@router.post("/stem-packs/{pack_id}/stems")
async def add_stem(
    pack_id: uuid.UUID,
    file: UploadFile = File(...),
    label: str = Form(...),
    duration: int = Form(...),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    data = StemCreate(label=label, duration=duration)
    stem = await stem_pack_service.add_stem_to_pack(db, pack_id, file, data)
    return success(StemResponse.model_validate(stem).model_dump(), "Stem added")


@router.put("/stem-packs/{pack_id}")
async def update_stem_pack(
    pack_id: uuid.UUID,
    body: StemPackCreate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
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


# --- Drone pad endpoints (admin mirror) ---

@router.post("/drones/categories")
async def create_drone_category(
    body: DronePadCategoryCreate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    import structlog as _structlog
    category = await drone_service.create_category(db, body, admin.id)
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
    admin=Depends(require_admin),
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
    admin=Depends(require_admin),
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
    drone = await drone_service.create_drone(db, file, data, admin.id, thumbnail=thumbnail)
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
    admin=Depends(require_admin),
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
        db, files, validated_keys, title, price, is_free, category_id, admin.id,
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
    admin=Depends(require_admin),
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
    admin=Depends(require_admin),
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
    admin=Depends(require_admin),
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
    admin=Depends(require_admin),
):
    pad = await drone_service.replace_pad_audio(db, drone_id, pad_id, file)
    import structlog as _structlog
    try:
        await cache_service.delete_pattern("drone:list:*")
    except Exception as e:
        _structlog.get_logger().warning("cache_invalidation_failed", endpoint="replace_drone_pad_audio", error=str(e))
    process_drone_upload.delay(str(pad_id))
    return success({"pad_id": str(pad.id), "status": pad.status}, "Pad audio replacement queued")


# --- Drum kit endpoints (admin mirror) ---

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
        403: {"description": "Admin role required"},
        422: {"description": "Validation error — missing price for paid kit, or sample/label count mismatch"},
    },
    status_code=201,
)
async def create_drum_kit(
    thumbnail: UploadFile | None = File(None),
    title: str = Form(...),
    description: str | None = Form(None),
    tags: str = Form(""),
    is_free: bool = Form(True),
    price: Decimal | None = Form(None),
    sample_files: list[UploadFile] = File(...),
    sample_labels: str = Form(...),  # comma-separated labels matching sample_files order
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
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
        db, data, admin.id, sample_files, labels, thumbnail=thumbnail
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
    summary="List drum kits (admin)",
    description="Paginated list of all drum kits with samples. Supports the same filters as the public endpoint.",
    response_description="Paginated drum kit list",
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
    },
)
async def list_drum_kits_admin(
    search: str | None = None,
    is_free: bool | None = None,
    tags: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    import asyncio as _asyncio
    from app.routers.drum_kits import _kit_to_dict
    filters = DrumKitFilter(
        search=search,
        is_free=is_free,
        tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None,
        page=page,
        page_size=page_size,
    )
    kits, total = await drum_kit_service.list_drum_kits(db, filters)
    return success({
        "items": list(await _asyncio.gather(*[_kit_to_dict(k) for k in kits])),
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.put("/drum-kits/{kit_id}")
async def update_drum_kit(
    kit_id: uuid.UUID,
    thumbnail: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    data = DrumKitUpdate(title=title, description=description, price=price, is_free=is_free)
    kit = await drum_kit_service.update_drum_kit(db, kit_id, data, thumbnail=thumbnail)
    return success(DrumKitResponse.model_validate(kit).model_dump(), "Drum kit updated")


@router.patch("/drum-kits/{kit_id}/samples/{sample_id}")
async def replace_drum_sample_audio(
    kit_id: uuid.UUID,
    sample_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    sample = await drum_kit_service.replace_sample_audio(db, kit_id, sample_id, file)
    process_drum_sample_upload.delay(str(sample_id))
    return success({"sample_id": str(sample.id), "status": sample.status}, "Sample audio replacement queued")
