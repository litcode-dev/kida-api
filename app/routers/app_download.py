from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.rate_limit import limiter
from app.schemas.app_download import AppDownloadRequestBody
from app.schemas.common import success
from app.services import app_download_service
from app.services.app_download_service import OS_LABELS
from app.services.email_service import send_email, app_download_html, app_download_text

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
    link = app_download_service.build_download_link(req.token)
    os_label = OS_LABELS[body.os]
    await send_email(
        to=body.email,
        subject=f"Your Kida download link for {os_label}",
        html=app_download_html(os_label, link, req.expires_at),
        text=app_download_text(os_label, link, req.expires_at),
    )
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
