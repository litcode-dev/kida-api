from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.rate_limit import limiter
from app.schemas.app_download import AppDownloadRequestBody, DesktopAppPublic
from app.schemas.common import success
from app.services import app_download_service
from app.tasks.notification_tasks import send_app_download_email

router = APIRouter(prefix="/app", tags=["app"])


@router.get(
    "/apps",
    summary="List apps available for download",
    description="Public endpoint. Active apps with their OS and price; never the file location.",
)
async def list_apps(db: AsyncSession = Depends(get_db)):
    apps = await app_download_service.list_apps(db, active_only=True)
    return success([DesktopAppPublic.model_validate(a).model_dump(mode="json") for a in apps])


@router.post(
    "/download-request",
    summary="Request a desktop app download link",
    description=(
        "Public endpoint. Submit an email address, operating system and optionally "
        "an `app_name` published by an admin.\n\n"
        "- **Free app** (or no `app_name`): a download link is emailed straight "
        "away. It expires after 3 days.\n"
        "- **Paid app**: the response carries `checkout_url`. Once the payment "
        "succeeds a download link, valid for 3 days, is emailed."
    ),
    responses={
        200: {"description": "Download link sent, or checkout started for a paid app"},
        404: {"description": "No active app with this name for this OS"},
        422: {"description": "Invalid email or unsupported OS"},
        503: {"description": "No payment gateway configured for the app's currency"},
    },
)
@limiter.limit("5/hour")
async def request_download(
    request: Request,
    body: AppDownloadRequestBody,
    db: AsyncSession = Depends(get_db),
):
    result = await app_download_service.request_download(
        db, body.email, body.os, app_name=body.app_name, provider=body.provider,
    )
    app = result.app
    data = {
        "email": body.email,
        "os": body.os,
        "app_name": app.name if app else app_download_service.DEFAULT_APP_NAME,
        "payment_required": result.checkout_url is not None,
    }
    if result.checkout_url is not None:
        req = result.request
        data.update({
            "checkout_url": result.checkout_url,
            "payment_reference": req.payment_reference,
            "payment_provider": req.payment_provider,
            "amount": str(req.amount),
            "currency": req.currency,
        })
        return success(
            message="Complete payment to receive your download link by email",
            data=data,
        )

    if result.request is not None:
        send_app_download_email.delay(str(result.request.id))
    return success(message="Download link sent", data=data)


@router.get(
    "/download/{token}",
    summary="Redeem a desktop app download link",
    description=(
        "Public endpoint. Redirects to a short-lived installer download URL when the "
        "token is valid and unexpired."
    ),
    responses={
        302: {"description": "Redirect to the installer download"},
        402: {"description": "Payment for this download has not been completed"},
        404: {"description": "Unknown download link"},
        410: {"description": "Download link has expired, or the app was removed"},
    },
)
async def redeem_download(token: str, db: AsyncSession = Depends(get_db)):
    url = await app_download_service.redeem(db, token)
    return RedirectResponse(url, status_code=302)
