"""Guards against the per-row query patterns the drone download paths used to have.

The group download endpoints resolve entitlement for many pads at once. Doing
that per pad turned a single request into one purchase lookup per pad, plus one
subscription lookup per drone. These tests pin the query counts so a future
refactor cannot quietly reintroduce that.
"""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event

from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.user import User, UserRole
from app.services import drone_service
from app.services.auth_service import hash_password
from tests.conftest import test_engine


async def _create_user(db):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Test",
        role=UserRole.user,
    )
    db.add(user)
    await db.commit()
    return user


async def _create_drone_with_pads(db, user_id, title, keys, is_free=True):
    drone = Drone(
        id=uuid.uuid4(),
        title=title,
        price=Decimal("0.00") if is_free else Decimal("5.00"),
        is_free=is_free,
        created_by=user_id,
    )
    db.add(drone)
    await db.flush()
    for key in keys:
        db.add(
            DronePad(
                id=uuid.uuid4(),
                drone_id=drone.id,
                key=key,
                duration=30,
                status="ready",
                file_s3_key=f"drones/{uuid.uuid4()}.wav",
            )
        )
    await db.commit()
    return drone


class _QueryCounter:
    """Counts SELECTs against a table for the duration of a block."""

    def __init__(self, table: str):
        self.table = table
        self.statements: list[str] = []

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        if self.table in statement and statement.lstrip().upper().startswith("SELECT"):
            self.statements.append(statement)

    def __enter__(self):
        event.listen(test_engine.sync_engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        event.remove(test_engine.sync_engine, "before_cursor_execute", self._on_execute)
        return False

    def __len__(self):
        return len(self.statements)


@pytest.mark.asyncio
async def test_group_download_uses_one_purchase_query_for_many_pads(db_session):
    user = await _create_user(db_session)
    keys = [MusicalKey.C, MusicalKey.D, MusicalKey.E, MusicalKey.F, MusicalKey.G]
    await _create_drone_with_pads(db_session, user.id, "Wide Pad", keys, is_free=False)

    with patch(
        "app.services.s3_service.get_download_url",
        new=AsyncMock(return_value="https://signed.url/f.wav"),
    ):
        with _QueryCounter("purchases") as counter:
            await drone_service.get_title_downloads(db_session, user, "Wide Pad")

    assert len(counter) == 1, (
        f"expected 1 purchase query for {len(keys)} pads, got {len(counter)}"
    )


@pytest.mark.asyncio
async def test_group_download_checks_subscription_once_across_drones(db_session):
    """Several drones share a title; the subscription answer is the same for all."""
    user = await _create_user(db_session)
    for n in range(4):
        await _create_drone_with_pads(
            db_session, user.id, "Shared Title", [MusicalKey.C, MusicalKey.D]
        )

    with patch(
        "app.services.s3_service.get_download_url",
        new=AsyncMock(return_value="https://signed.url/f.wav"),
    ):
        with _QueryCounter("iap_subscriptions") as counter:
            await drone_service.get_title_downloads(db_session, user, "Shared Title")

    assert len(counter) == 1, (
        f"expected 1 subscription query across 4 drones, got {len(counter)}"
    )


@pytest.mark.asyncio
async def test_query_count_does_not_grow_with_pad_count(db_session):
    """The whole point: cost is flat in the number of pads."""
    user = await _create_user(db_session)
    await _create_drone_with_pads(
        db_session, user.id, "Small", [MusicalKey.C], is_free=False
    )
    await _create_drone_with_pads(
        db_session,
        user.id,
        "Large",
        [MusicalKey.C, MusicalKey.D, MusicalKey.E, MusicalKey.F, MusicalKey.G],
        is_free=False,
    )

    async def _count(title: str) -> int:
        with patch(
            "app.services.s3_service.get_download_url",
            new=AsyncMock(return_value="https://signed.url/f.wav"),
        ):
            with _QueryCounter("purchases") as counter:
                await drone_service.get_title_downloads(db_session, user, title)
        return len(counter)

    assert await _count("Small") == await _count("Large")


@pytest.mark.asyncio
async def test_paid_pads_still_excluded_without_a_purchase(db_session):
    """The batched lookup must not become a way in — no purchase, no download."""
    user = await _create_user(db_session)
    await _create_drone_with_pads(
        db_session, user.id, "Paid Pad", [MusicalKey.C, MusicalKey.D], is_free=False
    )

    with patch(
        "app.services.s3_service.get_download_url",
        new=AsyncMock(return_value="https://signed.url/f.wav"),
    ):
        items = await drone_service.get_title_downloads(db_session, user, "Paid Pad")

    assert items == []
