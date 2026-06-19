from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.rate_limit import limiter
from app.schemas.app_download import AppDownloadRequestBody
from app.schemas.common import success
from app.services import app_download_service
from app.tasks.notification_tasks import send_app_download_email

router = APIRouter(prefix="/app", tags=["app"])


@router.post(
    "/download-request",
    summary="Request a desktop app download link",
    description=(
        "Public endpoint. Submit an email address and operating system to receive "
        "a download link for the Kida desktop app. The link expires after 3 days."
    ),
    responses={
        200: {"description": "Download link sent"},
        422: {"description": "Invalid email or unsupported OS"},
    },
)
@limiter.limit("5/hour")
async def request_download(
    request: Request,
    body: AppDownloadRequestBody,
    db: AsyncSession = Depends(get_db),
):
    req = await app_download_service.create_request(db, body.email, body.os)
    if req is not None:
        send_app_download_email.delay(str(req.id))
    return success(
        message="Download link sent",
        data={"email": body.email, "os": body.os},
    )


@router.get(
    "/download/{token}",
    summary="Redeem a desktop app download link",
    description=(
        "Public endpoint. Redirects to a short-lived installer download URL when the "
        "token is valid and unexpired."
    ),
    responses={
        302: {"description": "Redirect to the installer download"},
        404: {"description": "Unknown download link"},
        410: {"description": "Download link has expired"},
    },
)
async def redeem_download(token: str, db: AsyncSession = Depends(get_db)):
    url = await app_download_service.redeem(db, token)
    return RedirectResponse(url, status_code=302)
