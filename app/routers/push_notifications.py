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
    player_id: str = Field(
        ...,
        description=(
            "OneSignal push subscription ID (the v5 SDK's "
            "`OneSignal.User.pushSubscription.id`; older SDKs call it the player ID)"
        ),
    )


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
    description=(
        "Pushes are addressed to the user's external ID, which the app sets with "
        "`OneSignal.login(<user id>)` after sign-in. The ID saved here is only a "
        "fallback for a user OneSignal does not know by external ID yet."
    ),
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
        404: {"description": "User not found, or OneSignal has no device for them"},
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

    result = await onesignal_service.send_to_user(
        str(user.id),
        title=body.title,
        message=body.message,
        data=body.data,
        image_url=body.image_url,
        subscription_id=user.onesignal_player_id,
    )
    if result["unreachable"]:
        raise HTTPException(status_code=404, detail="User has no device subscribed to push")
    return success(
        data={"onesignal_response": [r["body"] for r in result["responses"]]},
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
    description="Admin only. Sends to a list of user IDs. Skips users OneSignal has no device for.",
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

    users = (await db.execute(
        select(User.id, User.onesignal_player_id).where(User.id.in_(valid_ids))
    )).all()
    if not users:
        raise HTTPException(status_code=404, detail="None of the specified users exist")

    result = await onesignal_service.send_to_external_ids(
        [str(uid) for uid, _ in users],
        title=body.title,
        message=body.message,
        data=body.data,
        image_url=body.image_url,
        fallback_subscription_ids={str(uid): sub for uid, sub in users if sub},
    )
    recipients = len(users) - len(result["unreachable"])
    if not recipients:
        raise HTTPException(
            status_code=404, detail="None of the specified users have a device subscribed to push"
        )
    return success(
        data={
            "recipients": recipients,
            "unreachable_user_ids": result["unreachable"],
            "onesignal_response": [r["body"] for r in result["responses"]],
        },
        message=f"Notification sent to {recipients} user(s)",
    )
