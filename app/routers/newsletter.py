from html import escape

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.newsletter import NewsletterSubscriber
from app.schemas.common import success
from app.services import newsletter_service, unsubscribe_service
from app.services.email_service import (
    send_email,
    newsletter_subscribe_html,
    newsletter_subscribe_text,
    newsletter_unsubscribe_html,
    newsletter_unsubscribe_text,
)

import structlog

log = structlog.get_logger()

router = APIRouter(prefix="/newsletter", tags=["newsletter"])


class SubscribeRequest(BaseModel):
    email: EmailStr


@router.post(
    "/subscribe",
    summary="Subscribe to newsletter",
    description="Subscribe an email address to Kida's news and marketing emails. Re-subscribing a previously unsubscribed address reactivates it.",
    responses={
        200: {"description": "Subscribed successfully"},
        422: {"description": "Invalid email address"},
    },
)
async def subscribe(body: SubscribeRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(
        select(NewsletterSubscriber).where(NewsletterSubscriber.email == body.email)
    )

    if existing:
        if existing.is_active:
            return success(message="Already subscribed", data={"email": body.email})
        existing.is_active = True
        existing.unsubscribed_at = None
        await db.commit()
        await send_email(
            to=body.email,
            subject="You're back — Kida newsletter",
            html=newsletter_subscribe_html(body.email),
            text=newsletter_subscribe_text(body.email),
        )
        return success(message="Subscription reactivated", data={"email": body.email})

    subscriber = NewsletterSubscriber(email=body.email)
    db.add(subscriber)
    await db.commit()
    await send_email(
        to=body.email,
        subject="You're subscribed to Kida",
        html=newsletter_subscribe_html(body.email),
        text=newsletter_subscribe_text(body.email),
    )
    return success(message="Subscribed successfully", data={"email": body.email})


@router.post(
    "/unsubscribe",
    summary="Unsubscribe from newsletter",
    description="Unsubscribe an email address from Kida's news and marketing emails.",
    responses={
        200: {"description": "Unsubscribed successfully or address not found"},
        422: {"description": "Invalid email address"},
    },
)
async def unsubscribe(body: SubscribeRequest, db: AsyncSession = Depends(get_db)):
    changed = await newsletter_service.unsubscribe(db, body.email)
    if not changed:
        return success(message="Already unsubscribed", data={"email": body.email})

    await send_email(
        to=body.email,
        subject="You've been unsubscribed from Kida",
        html=newsletter_unsubscribe_html(body.email),
        text=newsletter_unsubscribe_text(body.email),
    )
    return success(message="Unsubscribed successfully", data={"email": body.email})


_ONE_CLICK_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title></head>
<body style="margin:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:460px;margin:12vh auto;background:#fff;border-radius:8px;padding:36px 32px;text-align:center;">
    <p style="margin:0 0 10px 0;color:#1FBF62;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">{eyebrow}</p>
    <h1 style="margin:0 0 12px 0;font-size:20px;color:#0a0a0a;">{heading}</h1>
    <p style="margin:0;font-size:14px;line-height:1.6;color:#555;">{body}</p>
  </div>
</body></html>"""


def _one_click_page(title: str, eyebrow: str, heading: str, body: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        _ONE_CLICK_PAGE.format(title=title, eyebrow=eyebrow, heading=heading, body=body),
        status_code=status,
    )


_ONE_CLICK_DOC = (
    "The address in a marketing email's footer and its `List-Unsubscribe` header. "
    "GET is what a person clicking the link hits and returns a confirmation page; "
    "POST is what a mail client sends on their behalf under RFC 8058. No login, no "
    "confirmation step — one click and the address stops receiving marketing mail.\n\n"
    "`token` is an HMAC of the address, so an address can only be unsubscribed by "
    "someone holding a link that was actually sent to it."
)
_ONE_CLICK_RESPONSES = {
    200: {"description": "Unsubscribed, or already unsubscribed"},
    400: {"description": "Missing or invalid token"},
}


async def _unsubscribe_one_click(email: str, token: str, db: AsyncSession) -> HTMLResponse:
    if not unsubscribe_service.verify_token(email, token):
        log.warning("newsletter.one_click.bad_token", email=email)
        return _one_click_page(
            title="Link not valid",
            eyebrow="Something went wrong",
            heading="This unsubscribe link is not valid",
            body=(
                "It may have been altered in transit. Email "
                "support@litcode.com.ng and we will take you off the list."
            ),
            status=400,
        )

    changed = await newsletter_service.unsubscribe(db, email)
    log.info("newsletter.one_click.unsubscribed", email=email, changed=changed)
    return _one_click_page(
        title="Unsubscribed",
        eyebrow="Unsubscribed",
        heading="You're off the list",
        body=(
            f"{escape(email)} will no longer receive new-content emails from Kida. "
            "Purchase receipts and account emails are unaffected."
        ),
        status=200,
    )


# Two decorators rather than one api_route serving both methods: FastAPI
# derives an operation id per method, so a single handler on GET and POST
# emits the same id twice and collides in generated clients.
@router.get(
    "/unsubscribe/one-click",
    summary="One-click unsubscribe",
    description=_ONE_CLICK_DOC,
    responses=_ONE_CLICK_RESPONSES,
)
async def unsubscribe_one_click(
    email: str, token: str, db: AsyncSession = Depends(get_db)
):
    return await _unsubscribe_one_click(email, token, db)


@router.post(
    "/unsubscribe/one-click",
    summary="One-click unsubscribe (RFC 8058)",
    description="What a mail client POSTs to the `List-Unsubscribe` URL itself.",
    responses=_ONE_CLICK_RESPONSES,
)
async def unsubscribe_one_click_post(
    email: str, token: str, db: AsyncSession = Depends(get_db)
):
    return await _unsubscribe_one_click(email, token, db)
