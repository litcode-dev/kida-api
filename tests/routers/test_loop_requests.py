import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.models.loop_request import LoopRequest
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


async def _create_user(db):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Requester",
        role=UserRole.user,
    )
    db.add(user)
    await db.commit()
    return user


def _auth_headers(user):
    token = create_access_token(str(user.id), user.role.value)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def notify_task(monkeypatch):
    """Keep the admin notification off the real broker for every test here."""
    import app.routers.loop_requests as mod

    task = MagicMock()
    monkeypatch.setattr(mod, "send_loop_request_admin_notification", task)
    return task


@pytest.mark.asyncio
async def test_user_can_submit_loop_request(client, db_session, notify_task):
    user = await _create_user(db_session)

    response = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "loop",
            "artist_name": "  Tems  ",
            "song_title": "  Love Me JeJe  ",
            "reference_link": "https://example.com/reference",
            "notes": "  Please make it mellow.  ",
        },
        headers=_auth_headers(user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "success"
    assert body["message"] == "Loop request submitted"
    assert body["data"]["request_type"] == "loop"
    assert body["data"]["artist_name"] == "Tems"
    assert body["data"]["song_title"] == "Love Me JeJe"
    assert body["data"]["reference_link"] == "https://example.com/reference"
    assert body["data"]["notes"] == "Please make it mellow."

    saved = await db_session.scalar(select(LoopRequest))
    assert saved.user_id == user.id
    assert saved.request_type == "loop"
    assert saved.artist_name == "Tems"
    assert saved.song_title == "Love Me JeJe"

    notify_task.delay.assert_called_once_with(str(saved.id))


@pytest.mark.asyncio
async def test_loop_request_requires_authentication(client):
    response = await client.post(
        "/api/v1/loop-requests",
        json={"request_type": "loop", "artist_name": "Tems", "song_title": "Love Me JeJe"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_loop_request_validates_required_fields_and_link(client, db_session):
    user = await _create_user(db_session)

    missing_title = await client.post(
        "/api/v1/loop-requests",
        json={"request_type": "loop", "artist_name": "Tems"},
        headers=_auth_headers(user),
    )
    invalid_link = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "stems",
            "artist_name": "Tems",
            "song_title": "Love Me JeJe",
            "reference_link": "not-a-url",
        },
        headers=_auth_headers(user),
    )

    assert missing_title.status_code == 422
    assert invalid_link.status_code == 422


@pytest.mark.asyncio
async def test_loop_request_rejects_unknown_request_type(client, db_session):
    user = await _create_user(db_session)

    response = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "drum-kit",
            "artist_name": "Tems",
            "song_title": "Love Me JeJe",
        },
        headers=_auth_headers(user),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_invalid_request_does_not_notify_the_team_inbox(
    client, db_session, notify_task
):
    user = await _create_user(db_session)

    response = await client.post(
        "/api/v1/loop-requests",
        json={"request_type": "loop", "artist_name": "Tems"},
        headers=_auth_headers(user),
    )

    assert response.status_code == 422
    notify_task.delay.assert_not_called()


@pytest.mark.asyncio
async def test_request_is_saved_even_when_the_broker_is_down(
    client, db_session, notify_task
):
    user = await _create_user(db_session)
    notify_task.delay.side_effect = RuntimeError("broker unreachable")

    response = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "stems",
            "artist_name": "Tems",
            "song_title": "Love Me JeJe",
        },
        headers=_auth_headers(user),
    )

    assert response.status_code == 201
    assert await db_session.scalar(select(LoopRequest)) is not None


def test_notification_task_emails_the_admin_inbox(monkeypatch):
    """The task itself, over fakes.

    It ends in ``asyncio.run``, so it cannot run inside an async test — and the
    row it needs is a plain attribute read, which a stub session gives without a
    database.
    """
    import types
    from datetime import datetime, timezone

    from app.config import get_settings
    from app.models.user import User
    from app.tasks import notification_tasks

    user_id = uuid.uuid4()
    loop_request = types.SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        request_type="stems",
        artist_name="Tems",
        song_title="Love Me JeJe",
        reference_link="https://example.com/reference",
        notes="Please make it mellow.",
        created_at=datetime(2026, 8, 26, 16, 45, tzinfo=timezone.utc),
    )
    user = User(
        id=user_id,
        email="ada@test.com",
        password_hash="x",
        full_name="Ada Lovelace",
        role=UserRole.user,
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, pk):
            return user if model is User else loop_request

    sent = {}

    async def fake_send_email(*, to, subject, html, text):
        sent.update(to=to, subject=subject, html=html, text=text)

    monkeypatch.setattr("app.services.email_service.send_email", fake_send_email)
    monkeypatch.setattr("app.database.AsyncSessionLocal", lambda: _Session())

    notification_tasks.send_loop_request_admin_notification(str(loop_request.id))

    assert sent["to"] == get_settings().admin_notification_email
    assert sent["to"] == "kida.audio@gmail.com"
    assert sent["subject"] == "Stems request — Love Me JeJe by Tems"
    assert "Love Me JeJe" in sent["html"]
    assert "Ada Lovelace" in sent["html"]
    assert "ada@test.com" in sent["text"]
    assert "https://example.com/reference" in sent["text"]


def test_notification_task_skips_when_no_admin_inbox_is_configured(monkeypatch):
    from app.config import get_settings
    from app.tasks import notification_tasks

    monkeypatch.setenv("ADMIN_NOTIFICATION_EMAIL", "")
    get_settings.cache_clear()

    def _boom():
        raise AssertionError("no session should be opened without a recipient")

    monkeypatch.setattr("app.database.AsyncSessionLocal", _boom)
    try:
        notification_tasks.send_loop_request_admin_notification(str(uuid.uuid4()))
    finally:
        get_settings.cache_clear()
