from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.middleware.auth_middleware import get_current_user, require_admin
from app.models.user import User
from app.schemas.common import success
from app.services import onesignal_service

router = APIRouter(prefix="/push", tags=["push-notifications"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterDeviceRequest(BaseModel):
    player_id: str = Field(..., description="OneSignal player/device ID from the mobile app")


class SendToUserRequest(BaseModel):
    user_id: str = Field(..., description="Target user UUID")
    title: str
    message: str
    data: dict | None = None
    image_url: str | None = None


class BroadcastRequest(BaseModel):
    title: str
    message: str
    data: dict | None = None
    image_url: str | None = None


class SendToSegmentRequest(BaseModel):
    segment: str = Field(..., description="OneSignal segment name, e.g. 'Active Users'")
    title: str
    message: str
    data: dict | None = None
    image_url: str | None = None


# ── User endpoints ────────────────────────────────────────────────────────────

@router.post(
    "/register-device",
    summary="Register device for push notifications",
    description="Saves the OneSignal player ID for the authenticated user so they can receive push notifications.",
)
async def register_device(
    body: RegisterDeviceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.onesignal_player_id = body.player_id
    await db.commit()
    return success(message="Device registered for push notifications")


@router.delete(
    "/register-device",
    summary="Unregister device from push notifications",
    description="Removes the OneSignal player ID for the authenticated user.",
)
async def unregister_device(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.onesignal_player_id = None
    await db.commit()
    return success(message="Device unregistered from push notifications")


# ── Admin endpoints ───────────────────────────────────────────────────────────

@router.post(
    "/send/user",
    summary="Send push notification to a specific user",
    description="Admin only. Sends a push notification to a single user by their user ID.",
    responses={
        404: {"description": "User not found or has no registered device"},
        403: {"description": "Admin role required"},
    },
)
async def send_to_user(
    body: SendToUserRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    import uuid as _uuid
    user = await db.get(User, _uuid.UUID(body.user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.onesignal_player_id:
        raise HTTPException(status_code=404, detail="User has no registered device")

    result = await onesignal_service.send_notification(
        player_id=user.onesignal_player_id,
        title=body.title,
        message=body.message,
        data=body.data,
        image_url=body.image_url,
    )
    return success(
        data={"onesignal_response": result["body"]},
        message="Notification sent",
    )


@router.post(
    "/send/broadcast",
    summary="Broadcast push notification to all users",
    description="Admin only. Sends a push notification to all subscribed devices via OneSignal's 'All' segment.",
    responses={403: {"description": "Admin role required"}},
)
async def broadcast(
    body: BroadcastRequest,
    _: User = Depends(require_admin),
):
    result = await onesignal_service.send_to_all(
        title=body.title,
        message=body.message,
        data=body.data,
        image_url=body.image_url,
    )
    return success(
        data={"onesignal_response": result["body"]},
        message="Broadcast notification queued",
    )


@router.post(
    "/send/segment",
    summary="Send push notification to a OneSignal segment",
    description="Admin only. Sends to a named OneSignal segment (e.g. 'Active Users', 'Inactive Users').",
    responses={403: {"description": "Admin role required"}},
)
async def send_to_segment(
    body: SendToSegmentRequest,
    _: User = Depends(require_admin),
):
    result = await onesignal_service.send_to_segment(
        segment=body.segment,
        title=body.title,
        message=body.message,
        data=body.data,
        image_url=body.image_url,
    )
    return success(
        data={"onesignal_response": result["body"]},
        message=f"Notification sent to segment '{body.segment}'",
    )


@router.post(
    "/send/users",
    summary="Send push notification to multiple users",
    description="Admin only. Sends to a list of user IDs. Skips users without a registered device.",
    responses={403: {"description": "Admin role required"}},
)
async def send_to_users(
    body: BroadcastRequest,
    user_ids: list[str] = Query(..., description="List of target user UUIDs"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    import uuid as _uuid
    valid_ids = []
    for uid in user_ids:
        try:
            valid_ids.append(_uuid.UUID(uid))
        except ValueError:
            pass

    users = await db.scalars(
        select(User).where(
            User.id.in_(valid_ids),
            User.onesignal_player_id.is_not(None),
        )
    )
    player_ids = [u.onesignal_player_id for u in users.all()]

    if not player_ids:
        raise HTTPException(status_code=404, detail="None of the specified users have registered devices")

    result = await onesignal_service.send_to_users(
        player_ids=player_ids,
        title=body.title,
        message=body.message,
        data=body.data,
        image_url=body.image_url,
    )
    return success(
        data={"recipients": len(player_ids), "onesignal_response": result["body"]},
        message=f"Notification sent to {len(player_ids)} device(s)",
    )
