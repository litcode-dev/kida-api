import structlog
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.models.loop_request import LoopRequest
from app.models.user import User
from app.schemas.common import success
from app.schemas.loop_request import (
    LoopRequestCreate,
    LoopRequestResponse,
    LoopRequestStatus,
    LoopRequestType,
)
from app.tasks.notification_tasks import send_loop_request_admin_notification

log = structlog.get_logger()

router = APIRouter(prefix="/loop-requests", tags=["loop-requests"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Request a loop based on an existing song",
    description=(
        "Saves a request for a loop inspired by a reference track. Include an artist, "
        "song title, request type (`loop` or `stems`), and—when available—a link to "
        "the reference. The Kida team inbox is notified by email."
    ),
    responses={
        201: {"description": "Loop request created"},
        401: {"description": "Authentication required"},
        422: {"description": "Invalid or incomplete request"},
    },
)
async def create_loop_request(
    body: LoopRequestCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    loop_request = LoopRequest(
        user_id=user.id,
        request_type=body.request_type,
        artist_name=body.artist_name,
        song_title=body.song_title,
        reference_link=str(body.reference_link) if body.reference_link else None,
        notes=body.notes,
    )
    db.add(loop_request)
    await db.commit()
    await db.refresh(loop_request)

    # The request is saved; a broker outage must not report that as a failure
    # the user would retry — it would only duplicate the row.
    try:
        send_loop_request_admin_notification.delay(str(loop_request.id))
    except Exception as exc:  # noqa: BLE001 - the request is already stored
        log.error(
            "loop_request_admin_email.enqueue_failed",
            loop_request_id=str(loop_request.id),
            error=str(exc),
        )

    return success(
        data=LoopRequestResponse.model_validate(loop_request).model_dump(mode="json"),
        message="Loop request submitted",
    )


@router.get(
    "",
    summary="List your own loop and stems requests",
    description=(
        "Returns the requests the caller has submitted, newest first, so the app "
        "can show what was already asked for and where each request stands "
        "instead of the user submitting it again."
    ),
    responses={
        200: {"description": "The caller's requests"},
        401: {"description": "Authentication required"},
    },
)
async def list_my_loop_requests(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    request_type: LoopRequestType | None = Query(None, description="Filter by type"),
    status_filter: LoopRequestStatus | None = Query(
        None, alias="status", description="Filter by status"
    ),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    filters = [LoopRequest.user_id == user.id]
    if request_type is not None:
        filters.append(LoopRequest.request_type == request_type)
    if status_filter is not None:
        filters.append(LoopRequest.status == status_filter)

    total = await db.scalar(
        select(func.count()).select_from(LoopRequest).where(*filters)
    )
    rows = await db.scalars(
        select(LoopRequest)
        .where(*filters)
        .order_by(LoopRequest.created_at.desc(), LoopRequest.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return success({
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            LoopRequestResponse.model_validate(row).model_dump(mode="json")
            for row in rows.all()
        ],
    })
