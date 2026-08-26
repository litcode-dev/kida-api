import structlog
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.models.loop_request import LoopRequest
from app.models.user import User
from app.schemas.common import success
from app.schemas.loop_request import LoopRequestCreate, LoopRequestResponse
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
