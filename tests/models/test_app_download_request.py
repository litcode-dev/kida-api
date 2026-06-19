import pytest
from datetime import datetime, timedelta, timezone

from app.models.app_download_request import AppDownloadRequest


@pytest.mark.asyncio
async def test_can_persist_and_read_back(db_session):
    req = AppDownloadRequest(
        email="user@test.com",
        os="macos",
        token="tok-abc",
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    db_session.add(req)
    await db_session.commit()

    fetched = await db_session.get(AppDownloadRequest, req.id)
    assert fetched is not None
    assert fetched.email == "user@test.com"
    assert fetched.os == "macos"
    assert fetched.token == "tok-abc"
    assert fetched.last_redeemed_at is None
    assert fetched.created_at is not None
