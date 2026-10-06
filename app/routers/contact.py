from fastapi import APIRouter, Request

from app.middleware.rate_limit import limiter
from app.schemas.common import success
from app.schemas.contact import ContactMessageCreate
from app.tasks.notification_tasks import send_contact_admin_notification

router = APIRouter(prefix="/contact", tags=["contact"])


@router.post(
    "",
    summary="Send a message to the Kida team",
    description=(
        "Public endpoint behind the website's contact form. The message is "
        "emailed to the team inbox with Reply-To set to the sender, so a reply "
        "goes straight back to them. Nothing is stored."
    ),
    responses={
        200: {"description": "Message accepted for delivery"},
        422: {"description": "Missing or invalid field, or offensive language"},
        429: {"description": "Too many messages from this address"},
    },
)
@limiter.limit("5/hour")
async def send_contact_message(request: Request, body: ContactMessageCreate):
    send_contact_admin_notification.delay(
        name=body.name,
        email=body.email,
        subject=body.subject,
        message=body.message,
    )
    return success(message="Message sent", data={"email": body.email})
