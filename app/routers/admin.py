from fastapi import APIRouter, Depends, Query, Request, UploadFile, File, Form
from pydantic import BaseModel, EmailStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from decimal import Decimal
from app.database import get_db
from app.middleware.auth_middleware import require_admin, get_redis
from app.middleware.rate_limit import limiter
from app.schemas.user import UserResponse, SuspendRequest
from app.schemas.common import success
from app.schemas.loop import LoopCreate, LoopUpdate, LoopResponse
from app.routers.stem_pack_management import build_stem_pack_router
from app.schemas.drone_pad import (
    DronePadCategoryCreate,
    DronePadCategoryResponse,
    DronePadCreate,
    DronePadUpdate,
    DroneResponse,
)
from app.schemas.drum_kit import DrumKitCreate, DrumKitFilter, DrumKitResponse, DrumKitUpdate
from app.schemas.producer_analytics import AnalyticsPeriod, AnalyticsParams
from app.schemas.broadcast import (
    BroadcastAudience,
    BroadcastPreviewRequest,
    BroadcastRequest,
)
from app.schemas.subscription import (
    AdminSubscriptionActivateRequest,
    AdminSubscriptionDeactivateRequest,
)
from app.models.user import outranks, User, UserRole
from app.models.loop import Genre, TempoFeel
from app.models.drone_pad import MusicalKey
from app.exceptions import NotFoundError, AppError, ForbiddenError
from redis.asyncio import Redis
from app.services import (
    auth_service, broadcast_service, loop_service, drone_service, drum_kit_service, cache_service,
)
from app.services.admin_analytics_service import get_platform_analytics
from app.tasks.notification_tasks import send_broadcast_email
from app.tasks.upload_tasks import process_drone_upload, process_drum_sample_upload, process_loop_upload
import uuid
from datetime import date

import structlog

log = structlog.get_logger()

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
@limiter.limit("5/minute")
async def test_email(request: Request, body: EmailTestRequest, _: User = Depends(require_admin)):
    from app.services.email_service import send_email, registration_html, registration_text
    await send_email(
        to=body.email,
        subject="Kida — Email test",
        html=registration_html("there"),
        text=registration_text("there"),
    )
    return success(message="Test email dispatched", data={"to": body.email})


@router.get(
    "/email/broadcast/recipients",
    summary="Count broadcast recipients",
    description=(
        "How many addresses a broadcast would reach for the given audience, without "
        "sending anything.\n\n"
        "- `users` — registered accounts\n"
        "- `subscribers` — active newsletter subscribers\n"
        "- `all` — both, deduplicated (default)\n\n"
        "Addresses that unsubscribed from the newsletter are excluded from every "
        "audience, including `users`."
    ),
    responses={403: {"description": "Admin role required"}},
)
@limiter.limit("30/minute")
async def broadcast_recipient_count(
    request: Request,
    audience: BroadcastAudience = BroadcastAudience.all,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    total = await broadcast_service.count_recipients(db, audience)
    return success({"audience": audience.value, "recipients": total})


@router.post(
    "/email/broadcast/preview",
    summary="Preview a broadcast",
    description=(
        "Renders the broadcast and sends it to a single address so it can be checked "
        "before going out. Also returns the recipient count the real send would reach. "
        "Nothing is sent to the audience."
    ),
    responses={
        403: {"description": "Admin role required"},
        422: {"description": "Invalid payload"},
    },
)
@limiter.limit("10/minute")
async def preview_broadcast(
    request: Request,
    body: BroadcastPreviewRequest,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    from app.services.email_service import send_email, broadcast_html, broadcast_text

    await send_email(
        to=body.to,
        subject=f"[Preview] {body.subject}",
        html=broadcast_html(body.subject, body.body, body.heading, body.cta_label, body.cta_url),
        text=broadcast_text(body.subject, body.body, body.heading, body.cta_label, body.cta_url),
    )
    total = await broadcast_service.count_recipients(db, body.audience)
    return success(
        {"to": body.to, "audience": body.audience.value, "would_reach": total},
        "Preview sent",
    )


@router.post(
    "/email/broadcast",
    summary="Send an email to all users",
    description=(
        "Queues an announcement to every address in the chosen audience. Sending "
        "happens in the background — the response reports how many recipients were "
        "resolved at request time, not how many were delivered; check worker logs "
        "for `broadcast_email.done`.\n\n"
        "Addresses that unsubscribed from the newsletter are always excluded. "
        "Preview first with `/admin/email/broadcast/preview` — a broadcast cannot be "
        "recalled once queued."
    ),
    responses={
        403: {"description": "Admin role required"},
        422: {"description": "Invalid payload"},
    },
)
@limiter.limit("3/minute")
async def send_broadcast(
    request: Request,
    body: BroadcastRequest,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    total = await broadcast_service.count_recipients(db, body.audience)
    if total == 0:
        raise AppError("No recipients for this audience", status_code=422)

    send_broadcast_email.delay(
        body.subject,
        body.body,
        body.audience.value,
        body.heading,
        body.cta_label,
        body.cta_url,
    )
    log.info(
        "admin.broadcast_queued",
        admin_id=str(admin.id),
        audience=body.audience.value,
        recipients=total,
        subject=body.subject,
    )
    return success(
        {"audience": body.audience.value, "recipients": total},
        f"Broadcast queued to {total} recipient(s)",
    )


# --- Loop endpoints ---

@router.delete("/loops/{loop_id}")
@limiter.limit("20/minute")
async def delete_loop(
    request: Request,
    loop_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    await loop_service.delete_loop(db, loop_id)
    return success(message="Loop deleted")


# --- StemPack endpoints ---
# Packs, song parts, stems and arrangements come from the same builder the
# producer router mounts; admins are not held to packs they created.
router.include_router(build_stem_pack_router(require_admin, enforce_ownership=False))


# --- User management endpoints (admin only) ---

@router.get("/users")
@limiter.limit("60/minute")
async def list_users(
    request: Request,
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


@router.put(
    "/users/{user_id}/role",
    summary="Change a user's role",
    description=(
        "Admins can move accounts between `user` and `producer`. Granting or "
        "revoking `admin` or `super_admin`, and changing the role of anyone who "
        "already holds one, requires a super admin.\n\n"
        "A super admin cannot change their own role or another super admin's — "
        "that would let the top role be removed by accident, or by a compromised "
        "session, with no way back."
    ),
    responses={
        403: {"description": "Caller does not outrank the target, or the role is above them"},
        404: {"description": "User not found"},
    },
)
@limiter.limit("30/minute")
async def change_user_role(
    request: Request,
    user_id: uuid.UUID,
    role: UserRole,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")
    if user.id == admin.id:
        raise ForbiddenError("Cannot change your own role")
    # Both ends matter: you must outrank who they are now, and the role you are
    # handing out — otherwise an admin could promote someone to super admin and
    # inherit that power through them.
    if not outranks(admin.role, user.role):
        raise ForbiddenError("Cannot change the role of an account at or above your own")
    if not outranks(admin.role, role):
        raise ForbiddenError("Cannot grant a role at or above your own")
    user.role = role
    await db.commit()
    await db.refresh(user)
    log.info(
        "admin.role_changed",
        admin_id=str(admin.id), user_id=str(user_id), new_role=role.value,
    )
    return success(UserResponse.model_validate(user).model_dump(), "Role updated")


# --- AI administration ---

@router.put("/users/{user_id}/ai-enabled")
@limiter.limit("30/minute")
async def toggle_user_ai(
    request: Request,
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


@router.put("/users/{user_id}/suspend")
@limiter.limit("10/minute")
async def suspend_user(
    request: Request,
    user_id: uuid.UUID,
    body: SuspendRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")
    # A super admin can suspend an admin; nobody can suspend an equal or higher
    # role, which also blocks suspending yourself.
    if not outranks(admin.role, user.role):
        raise ForbiddenError("Cannot suspend an account at or above your own role")
    user.is_suspended = True
    user.suspension_reason = body.reason
    await db.commit()
    await db.refresh(user)
    await redis.set(f"suspended:{user_id}", body.reason)
    from app.services.email_service import send_email, account_suspended_html, account_suspended_text
    await send_email(
        to=user.email,
        subject="Your Kida account has been suspended",
        html=account_suspended_html(user.full_name, body.reason),
        text=account_suspended_text(user.full_name, body.reason),
    )
    return success(UserResponse.model_validate(user).model_dump(), "User suspended")


@router.put("/users/{user_id}/unsuspend")
@limiter.limit("10/minute")
async def unsuspend_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")
    user.is_suspended = False
    user.suspension_reason = None
    await db.commit()
    await db.refresh(user)
    await redis.delete(f"suspended:{user_id}")
    from app.services.email_service import send_email, account_unsuspended_html, account_unsuspended_text
    await send_email(
        to=user.email,
        subject="Your Kida account has been reinstated",
        html=account_unsuspended_html(user.full_name),
        text=account_unsuspended_text(user.full_name),
    )
    return success(UserResponse.model_validate(user).model_dump(), "User unsuspended")


@router.delete(
    "/users/{user_id}",
    summary="Delete a user",
    description=(
        "Permanently deletes a user's account and all associated data: purchases, "
        "downloads, likes, AI generations, subscriptions and any producer content "
        "they created. The user is emailed a confirmation. This action is "
        "irreversible — use suspend instead to block access reversibly.\n\n"
        "An admin can only delete accounts below their own role: a super admin "
        "can delete an admin, an admin cannot, and nobody can delete a super "
        "admin or themselves."
    ),
    responses={
        403: {"description": "Caller does not outrank the target"},
        404: {"description": "User not found"},
    },
)
@limiter.limit("10/minute")
async def delete_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")
    # Rank comparison covers self-deletion too — nobody outranks themselves.
    if not outranks(admin.role, user.role):
        raise ForbiddenError("Cannot delete an account at or above your own role")

    email = user.email
    await auth_service.delete_user(db, user, redis, None, actor="admin")
    log.info("admin.user_deleted", admin_id=str(admin.id), user_id=str(user_id), email=email)
    return success(message="User deleted")


# --- Subscription administration ---

@router.get(
    "/users/{user_id}/subscription",
    summary="View a user's premium subscription",
    description=(
        "The user's Kiɗa Premium entitlement: whether it is currently active, the "
        "plan (monthly or yearly), when it expires, and whether it came from a store "
        "purchase or an admin grant. Returns `active: false` with null fields when the "
        "user has never subscribed."
    ),
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
        404: {"description": "User not found"},
    },
)
@limiter.limit("60/minute")
async def get_user_subscription(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    from app.services import iap_subscription_service
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")
    sub = await iap_subscription_service.get_subscription(db, user_id)
    return success(iap_subscription_service.admin_view(user_id, sub))


@router.put(
    "/users/{user_id}/subscription/activate",
    summary="Activate a user's premium subscription",
    description=(
        "Grants Kiɗa Premium on the `monthly` or `yearly` plan without a store purchase — "
        "for comps, support credits and testing. Also used to switch an existing subscriber "
        "between plans, since a user has a single entitlement.\n\n"
        "`expires_at` defaults to 30 days ahead for monthly and 365 for yearly; pass an "
        "explicit future timestamp to set a custom end date. Activating clears an earlier "
        "admin deactivation.\n\n"
        "This grants content entitlement only — the AI generation quota comes from a paid "
        "web subscription and is not affected."
    ),
    responses={
        400: {"description": "expires_at is not in the future"},
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
        404: {"description": "User not found"},
    },
)
@limiter.limit("30/minute")
async def activate_user_subscription(
    request: Request,
    user_id: uuid.UUID,
    body: AdminSubscriptionActivateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    from app.services import iap_subscription_service
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")
    sub = await iap_subscription_service.admin_activate(
        db, user_id, body.plan, expires_at=body.expires_at, note=body.note
    )
    return success(
        iap_subscription_service.admin_view(user_id, sub),
        f"{body.plan.value.capitalize()} subscription activated",
    )


@router.put(
    "/users/{user_id}/subscription/deactivate",
    summary="Deactivate a user's premium subscription",
    description=(
        "Revokes the user's Kiɗa Premium entitlement immediately: free-tier download caps "
        "apply again and any active web subscription (with its AI quota) is expired.\n\n"
        "The entitlement is kept and locked rather than deleted, so the record survives and "
        "the app cannot restore premium by re-posting its store receipt — receipt "
        "verification returns 403 until an admin activates the user again.\n\n"
        "This does not cancel the user's billing with Apple or Google; they must do that "
        "from their own store account, otherwise they keep being charged."
    ),
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
        404: {"description": "User not found"},
    },
)
@limiter.limit("30/minute")
async def deactivate_user_subscription(
    request: Request,
    user_id: uuid.UUID,
    body: AdminSubscriptionDeactivateRequest | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    from app.services import iap_subscription_service, subscription_service
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("User not found")

    note = body.note if body else None
    sub = await iap_subscription_service.admin_deactivate(db, user_id, note=note)
    web_sub = await subscription_service.expire_active_subscription(db, user_id)

    data = iap_subscription_service.admin_view(user_id, sub)
    data["web_subscription_expired"] = web_sub is not None
    return success(data, "Subscription deactivated")


@router.get("/ai/generations")
@limiter.limit("60/minute")
async def list_all_generations(
    request: Request,
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
@limiter.limit("20/minute")
async def delete_drone_category(
    request: Request,
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
@limiter.limit("20/minute")
async def delete_drone(
    request: Request,
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
@limiter.limit("20/minute")
async def delete_drum_kit(
    request: Request,
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
@limiter.limit("60/minute")
async def list_newsletter_subscribers(
    request: Request,
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
@limiter.limit("30/minute")
async def platform_analytics(
    request: Request,
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
@limiter.limit("10/minute")
async def upload_loop(
    request: Request,
    file: UploadFile = File(...),
    thumbnail: UploadFile | None = File(None),
    title: str = Form(...),
    genre: Genre = Form(...),
    bpm: int = Form(...),
    time_signature: str = Form("4/4"),
    tempo_feel: TempoFeel = Form(...),
    price: Decimal = Form(...),
    is_free: bool = Form(False),
    desired_price_usd: Decimal | None = Form(None),
    tags: str = Form(""),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    data = LoopCreate(
        title=title, genre=genre, bpm=bpm,
        time_signature=time_signature, tempo_feel=tempo_feel,
        price=price, is_free=is_free,
        desired_price_usd=desired_price_usd,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    loop = await loop_service.create_loop(db, file, data, admin.id, thumbnail=thumbnail)
    process_loop_upload.delay(str(loop.id))
    return success(LoopResponse.model_validate(loop).model_dump(), "Loop upload queued")


@router.get("/loops/{loop_id}/status")
@limiter.limit("60/minute")
async def loop_upload_status(
    request: Request,
    loop_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    loop = await loop_service.get_loop(db, loop_id)
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
    time_signature: str | None = Form(None),
    tempo_feel: TempoFeel | None = Form(None),
    tags: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    desired_price_usd: Decimal | None = Form(None),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    data = LoopUpdate(
        title=title,
        description=description,
        genre=genre,
        bpm=bpm,
        time_signature=time_signature,
        tempo_feel=tempo_feel,
        tags=tags_list,
        price=price,
        is_free=is_free,
        desired_price_usd=desired_price_usd,
    )
    loop, should_reprocess = await loop_service.update_loop(
        db, loop_id, data, thumbnail=thumbnail, file=file
    )
    if should_reprocess:
        process_loop_upload.delay(str(loop_id))
    return success(LoopResponse.model_validate(loop).model_dump(), "Loop updated")


# --- Drone pad endpoints (admin mirror) ---

@router.post("/drones/categories")
@limiter.limit("30/minute")
async def create_drone_category(
    request: Request,
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
@limiter.limit("60/minute")
async def list_drone_categories(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
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
    desired_price_usd: Decimal | None = Form(None),
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
        desired_price_usd=desired_price_usd,
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
@limiter.limit("60/minute")
async def drone_upload_status(
    request: Request,
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
@limiter.limit("20/minute")
async def update_drone(
    request: Request,
    drone_id: uuid.UUID,
    thumbnail: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    desired_price_usd: Decimal | None = Form(None),
    category_id: uuid.UUID | None = Form(None),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    data = DronePadUpdate(
        title=title,
        description=description,
        price=price,
        is_free=is_free,
        desired_price_usd=desired_price_usd,
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
@limiter.limit("10/minute")
async def create_drum_kit(
    request: Request,
    thumbnail: UploadFile | None = File(None),
    title: str = Form(...),
    description: str | None = Form(None),
    tags: str = Form(""),
    is_free: bool = Form(True),
    price: Decimal | None = Form(None),
    desired_price_usd: Decimal | None = Form(None),
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
        desired_price_usd=desired_price_usd,
    )
    kit, sample_ids = await drum_kit_service.create_drum_kit(
        db, data, admin.id, sample_files, labels, thumbnail=thumbnail
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
    summary="List drum kits (admin)",
    description="Paginated list of all drum kits with samples. Supports the same filters as the public endpoint.",
    response_description="Paginated drum kit list",
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
    },
)
@limiter.limit("60/minute")
async def list_drum_kits_admin(
    request: Request,
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
@limiter.limit("20/minute")
async def update_drum_kit(
    request: Request,
    kit_id: uuid.UUID,
    thumbnail: UploadFile | None = File(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    price: Decimal | None = Form(None),
    is_free: bool | None = Form(None),
    desired_price_usd: Decimal | None = Form(None),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    data = DrumKitUpdate(
        title=title, description=description, price=price, is_free=is_free,
        desired_price_usd=desired_price_usd,
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
    admin=Depends(require_admin),
):
    sample = await drum_kit_service.replace_sample_audio(db, kit_id, sample_id, file)
    process_drum_sample_upload.delay(str(sample_id))
    return success({"sample_id": str(sample.id), "status": sample.status}, "Sample audio replacement queued")


# --- IAP price sync administration ---

@router.get(
    "/price-sync/errors",
    summary="List items whose store price sync failed",
    description=(
        "Returns every sellable item (loop, drum kit, drone group) in "
        "`price_sync_state=error` with the recorded failure message. Re-setting "
        "`desired_price_usd` on an item flips it back to `pending` for retry."
    ),
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
    },
)
@limiter.limit("60/minute")
async def list_price_sync_errors(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    from app.services import price_sync_service
    return success(await price_sync_service.list_sync_errors(db))


@router.post(
    "/price-sync/run",
    summary="Trigger a store price sync run now",
    description="Queues the reconcile worker immediately instead of waiting for the next scheduled run.",
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
    },
)
@limiter.limit("10/minute")
async def trigger_price_sync(
    request: Request,
    _: User = Depends(require_admin),
):
    from app.tasks.price_sync_tasks import reconcile_store_prices
    reconcile_store_prices.delay()
    return success(message="Price sync run queued")


@router.post(
    "/price-sync/{store_product_id}/retire",
    summary="Retire a store product",
    description=(
        "Sets the Google Play product to inactive and removes the App Store product "
        "from sale. SKUs are never deleted or reused — retiring is the only way to "
        "take an item off the stores."
    ),
    responses={
        401: {"description": "Missing or invalid token"},
        403: {"description": "Admin role required"},
    },
)
@limiter.limit("10/minute")
async def retire_store_product(
    request: Request,
    store_product_id: str,
    _: User = Depends(require_admin),
):
    from app.services import price_sync_service
    await price_sync_service.retire_product(store_product_id)
    return success(message=f"Store product {store_product_id} retired")
