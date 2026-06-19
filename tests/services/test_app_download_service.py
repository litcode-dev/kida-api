import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from app.models.app_download_request import AppDownloadRequest
from app.services import app_download_service, s3_service
from app.exceptions import AppError, NotFoundError


@pytest.mark.asyncio
async def test_create_request_sets_3_day_expiry(db_session):
    req = await app_download_service.create_request(db_session, "user@test.com", "macos")
    assert req.token
    assert req.os == "macos"
    delta = req.expires_at - datetime.now(timezone.utc)
    assert timedelta(days=2, hours=23) < delta <= timedelta(days=3)


def test_build_download_link(monkeypatch):
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "api_base_url", "https://api.example/")
    link = app_download_service.build_download_link("tok123")
    assert link == "https://api.example/api/v1/app/download/tok123"


@pytest.mark.asyncio
async def test_redeem_valid_returns_presigned_url(db_session, monkeypatch):
    monkeypatch.setattr(s3_service, "generate_presigned_url", AsyncMock(return_value="https://s3/signed"))
    req = await app_download_service.create_request(db_session, "u@test.com", "windows")
    url = await app_download_service.redeem(db_session, req.token)
    assert url == "https://s3/signed"
    refreshed = await db_session.get(AppDownloadRequest, req.id)
    assert refreshed.last_redeemed_at is not None


@pytest.mark.asyncio
async def test_redeem_unknown_token_raises_not_found(db_session):
    with pytest.raises(NotFoundError):
        await app_download_service.redeem(db_session, "does-not-exist")


@pytest.mark.asyncio
async def test_redeem_expired_token_raises_410(db_session):
    req = AppDownloadRequest(
        email="e@test.com",
        os="macos",
        token="expired-token",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add(req)
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await app_download_service.redeem(db_session, "expired-token")
    assert exc.value.status_code == 410


@pytest.mark.asyncio
async def test_create_request_throttles_repeat_email(db_session):
    first = await app_download_service.create_request(db_session, "dup@test.com", "macos")
    assert first is not None
    second = await app_download_service.create_request(db_session, "dup@test.com", "macos")
    assert second is None

    from sqlalchemy import func
    count = await db_session.scalar(
        select(func.count())
        .select_from(AppDownloadRequest)
        .where(AppDownloadRequest.email == "dup@test.com")
    )
    assert count == 1
