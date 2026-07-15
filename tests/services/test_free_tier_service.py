"""Race-safety of free-tier grant issuance under concurrent requests."""
import asyncio
import uuid

import pytest
from sqlalchemy import select, func

from app.config import get_settings
from app.exceptions import FreeTierLimitError
from app.models.download_grant import DownloadGrant, DownloadGrantType
from app.models.user import User, UserRole
from app.services import free_tier_service
from app.services.auth_service import hash_password
from tests.conftest import TestSessionLocal


async def _create_user(db):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"), full_name="Test", role=UserRole.user,
    )
    db.add(user)
    await db.commit()
    return user


async def _grant_once(user_id, item_id):
    """Run ensure_grant in its own session/transaction, mirroring a request."""
    async with TestSessionLocal() as session:
        try:
            await free_tier_service.ensure_grant(
                session, user_id, DownloadGrantType.loop, item_id
            )
            await session.commit()
            return True
        except FreeTierLimitError:
            await session.rollback()
            return False


@pytest.mark.asyncio
async def test_concurrent_first_time_requests_do_not_over_grant(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "free_tier_loop_downloads", 2)
    user = await _create_user(db_session)

    # Three simultaneous first-time downloads of different loops; cap is 2.
    results = await asyncio.gather(
        _grant_once(user.id, "loop-a"),
        _grant_once(user.id, "loop-b"),
        _grant_once(user.id, "loop-c"),
    )

    assert sum(results) == 2  # exactly two succeed, one refused
    total = await db_session.scalar(
        select(func.count()).select_from(DownloadGrant).where(
            DownloadGrant.user_id == user.id,
            DownloadGrant.item_type == DownloadGrantType.loop,
        )
    )
    assert total == 2  # never over-granted


@pytest.mark.asyncio
async def test_concurrent_same_item_requests_grant_once(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "free_tier_loop_downloads", 2)
    user = await _create_user(db_session)

    results = await asyncio.gather(
        _grant_once(user.id, "loop-a"),
        _grant_once(user.id, "loop-a"),
    )

    assert all(results)  # both succeed (idempotent)
    total = await db_session.scalar(
        select(func.count()).select_from(DownloadGrant).where(
            DownloadGrant.user_id == user.id
        )
    )
    assert total == 1  # only one row
