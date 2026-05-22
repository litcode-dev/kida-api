from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.middleware.auth_middleware import require_admin
from app.services import loop_service, drone_service, drum_kit_service, cache_service
from app.schemas.user import UserResponse
from app.schemas.common import success
from app.models.user import User, UserRole
from app.exceptions import NotFoundError, AppError
import uuid
from datetime import date
from app.schemas.producer_analytics import AnalyticsPeriod, AnalyticsParams
from app.services.admin_analytics_service import get_platform_analytics

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
