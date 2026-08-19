"""The digest, visible and triggerable without a shell on the box.

"No mail arrived" used to require database access to investigate and a
deployment to act on. These two endpoints answer why, and send it now.
"""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models.loop import Genre, Loop, TempoFeel
from app.models.user import User, UserRole
from app.services import digest_sender, email_service
from app.services.auth_service import create_access_token, hash_password
from tests.conftest import TestSessionLocal

STATUS = "/api/v1/admin/email/digest"
RUN = "/api/v1/admin/email/digest/run"


async def _user(db, role=UserRole.user):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Test User",
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


async def _ready_loop(db, user_id, title="Fresh Loop"):
    loop = Loop(
        id=uuid.uuid4(),
        title=title,
        slug=f"loop-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        bpm=100,
        duration=30,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("10.00"),
        status="ready",
        created_by=user_id,
    )
    db.add(loop)
    await db.commit()
    return loop


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


@pytest.fixture
def digest_env():
    settings = digest_sender.get_settings().model_copy(update={
        "content_digest_enabled": True,
        "content_digest_hour_utc": 0,
        "email_backend": "resend",
        "resend_api_key": "test-key",
    })
    with patch.object(digest_sender, "AsyncSessionLocal", TestSessionLocal), \
            patch.object(digest_sender, "get_settings", return_value=settings):
        yield settings


@pytest.mark.asyncio
async def test_status_needs_an_admin(client, db_session):
    user = await _user(db_session)

    assert (await client.get(STATUS, headers=_headers(user))).status_code == 403


@pytest.mark.asyncio
async def test_status_reports_what_is_queued_and_who_would_get_it(client, db_session):
    admin = await _user(db_session, role=UserRole.admin)
    await _ready_loop(db_session, admin.id, title="Queued Loop")

    body = (await client.get(STATUS, headers=_headers(admin))).json()["data"]

    assert body["queued_items"] == 1
    assert body["queued"][0]["title"] == "Queued Loop"
    assert body["recipients"] == 1
    assert body["last_due_run_status"] == "never ran"


@pytest.mark.asyncio
async def test_status_names_the_missing_credentials(client, db_session):
    admin = await _user(db_session, role=UserRole.admin)
    unconfigured = email_service.get_settings().model_copy(
        update={"email_backend": "resend", "resend_api_key": ""}
    )

    # The endpoint imports get_settings at call time, so patching the source is
    # enough — no need to reach into the router's namespace.
    with patch("app.config.get_settings", return_value=unconfigured):
        body = (await client.get(STATUS, headers=_headers(admin))).json()["data"]

    assert "RESEND_API_KEY" in body["delivery_problem"]


@pytest.mark.asyncio
async def test_running_it_by_hand_sends_and_reports(client, db_session, digest_env):
    admin = await _user(db_session, role=UserRole.admin)
    loop = await _ready_loop(db_session, admin.id)

    with patch.object(
        email_service, "send_bulk_email", new=AsyncMock(return_value=(1, 0))
    ) as send:
        body = (await client.post(RUN, headers=_headers(admin))).json()["data"]

    send.assert_awaited_once()
    assert body["status"] == "sent"
    assert body["items"] == 1
    assert body["run_key"].startswith("manual:")

    await db_session.refresh(loop)
    assert loop.announced_at is not None


@pytest.mark.asyncio
async def test_the_run_shows_up_in_the_history(client, db_session, digest_env):
    admin = await _user(db_session, role=UserRole.admin)
    await _ready_loop(db_session, admin.id)

    with patch.object(email_service, "send_bulk_email", new=AsyncMock(return_value=(1, 0))):
        await client.post(RUN, headers=_headers(admin))

    body = (await client.get(STATUS, headers=_headers(admin))).json()["data"]

    assert [r["status"] for r in body["runs"]] == ["sent"]
    assert body["runs"][0]["trigger"] == "manual"


@pytest.mark.asyncio
async def test_running_it_needs_an_admin(client, db_session):
    user = await _user(db_session)

    assert (await client.post(RUN, headers=_headers(user))).status_code == 403
