import pytest
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import select

from app.models.app_download_request import AppDownloadRequest


@pytest.mark.asyncio
async def test_request_download_enqueues_email(client, monkeypatch):
    import app.routers.app_download as mod
    mock_task = MagicMock()
    monkeypatch.setattr(mod, "send_app_download_email", mock_task)

    resp = await client.post(
        "/api/v1/app/download-request",
        json={"email": "a@test.com", "os": "macos"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["data"]["email"] == "a@test.com"
    assert body["data"]["os"] == "macos"
    mock_task.delay.assert_called_once()


@pytest.mark.asyncio
async def test_request_download_rejects_unsupported_os(client):
    resp = await client.post(
        "/api/v1/app/download-request",
        json={"email": "a@test.com", "os": "linux"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_request_download_rejects_invalid_email(client):
    resp = await client.post(
        "/api/v1/app/download-request",
        json={"email": "not-an-email", "os": "macos"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_redeem_redirects_to_installer(client, db_session, monkeypatch):
    import app.routers.app_download as mod
    monkeypatch.setattr(mod, "send_app_download_email", MagicMock())
    from app.services import s3_service
    monkeypatch.setattr(
        s3_service, "generate_r2_presigned_url", AsyncMock(return_value="https://r2/installer.exe")
    )

    await client.post(
        "/api/v1/app/download-request",
        json={"email": "b@test.com", "os": "windows"},
    )
    req = await db_session.scalar(
        select(AppDownloadRequest).where(AppDownloadRequest.email == "b@test.com")
    )

    resp = await client.get(f"/api/v1/app/download/{req.token}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://r2/installer.exe"


@pytest.mark.asyncio
async def test_redeem_unknown_token_returns_404(client):
    resp = await client.get("/api/v1/app/download/nonexistent-token", follow_redirects=False)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_request_download_throttles_repeat_email(client, monkeypatch):
    import app.routers.app_download as mod
    mock_task = MagicMock()
    monkeypatch.setattr(mod, "send_app_download_email", mock_task)

    first = await client.post(
        "/api/v1/app/download-request",
        json={"email": "throttle@test.com", "os": "macos"},
    )
    second = await client.post(
        "/api/v1/app/download-request",
        json={"email": "throttle@test.com", "os": "macos"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()   # identical generic response, no enumeration
    assert mock_task.delay.call_count == 1  # enqueued only once
