import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import AppError, NotFoundError
from app.models.app_download_request import AppDownloadRequest
from app.services import s3_service

LINK_TTL_DAYS = 3
PRESIGN_TTL_SECONDS = 300

OS_LABELS = {"macos": "macOS", "windows": "Windows"}


def _installer_key(os: str) -> str:
    settings = get_settings()
    keys = {
        "macos": settings.app_installer_macos_s3_key,
        "windows": settings.app_installer_windows_s3_key,
    }
    key = keys.get(os)
    if not key:
        raise AppError("Installer is not available for the requested OS", status_code=503)
    return key


async def create_request(db: AsyncSession, email: str, os: str) -> AppDownloadRequest:
    """Persist a download request with a 3-day expiry and return it."""
    req = AppDownloadRequest(
        email=email,
        os=os,
        token=secrets.token_urlsafe(32),
        expires_at=datetime.now(timezone.utc) + timedelta(days=LINK_TTL_DAYS),
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)
    return req


def build_download_link(token: str) -> str:
    base = get_settings().api_base_url.rstrip("/")
    return f"{base}/api/v1/app/download/{token}"


async def redeem(db: AsyncSession, token: str) -> str:
    """Validate a token and return a short-lived presigned installer URL."""
    req = await db.scalar(
        select(AppDownloadRequest).where(AppDownloadRequest.token == token)
    )
    if req is None:
        raise NotFoundError("Invalid or unknown download link")
    if req.expires_at < datetime.now(timezone.utc):
        raise AppError("This download link has expired", status_code=410)

    key = _installer_key(req.os)
    url = await s3_service.generate_presigned_url(key, expiry_seconds=PRESIGN_TTL_SECONDS)

    req.last_redeemed_at = datetime.now(timezone.utc)
    await db.commit()
    return url
