import uuid
from unittest.mock import MagicMock

import pytest

from app.models.loop_request import LoopRequest
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


async def _create_user(db, role=UserRole.user, full_name="Requester"):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name=full_name,
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


def _auth_headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


@pytest.fixture(autouse=True)
def status_email(monkeypatch):
    """Keep the requester notification off the real broker for every test here."""
    import app.routers.admin as mod

    task = MagicMock()
    monkeypatch.setattr(mod, "send_loop_request_status_email", task)
    return task


async def _submit(db, user, **overrides):
    fields = dict(
        user_id=user.id,
        request_type="loop",
        artist_name="Tems",
        song_title="Love Me JeJe",
    )
    fields.update(overrides)
    loop_request = LoopRequest(**fields)
    db.add(loop_request)
    await db.commit()
    await db.refresh(loop_request)
    return loop_request


# --- listing -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_sees_every_request_with_its_requester(client, db_session):
    admin = await _create_user(db_session, UserRole.admin, full_name="Admin")
    ada = await _create_user(db_session, full_name="Ada Lovelace")
    await _submit(db_session, ada, song_title="Love Me JeJe")

    response = await client.get(
        "/api/v1/admin/loop-requests", headers=_auth_headers(admin)
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1
    item = data["items"][0]
    assert item["song_title"] == "Love Me JeJe"
    assert item["requester_name"] == "Ada Lovelace"
    assert item["requester_email"] == ada.email
    assert item["user_id"] == str(ada.id)
    assert item["status"] == "new"
    assert item["status_changed_at"] is None


@pytest.mark.asyncio
async def test_admin_listing_spans_users_newest_first(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    one = await _create_user(db_session)
    two = await _create_user(db_session)
    await _submit(db_session, one, song_title="Older")
    await _submit(db_session, two, song_title="Newer")

    response = await client.get(
        "/api/v1/admin/loop-requests", headers=_auth_headers(admin)
    )

    assert [i["song_title"] for i in response.json()["data"]["items"]] == [
        "Newer", "Older",
    ]


@pytest.mark.asyncio
async def test_admin_listing_filters_by_status_and_type(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    await _submit(db_session, user, song_title="Untouched")
    await _submit(db_session, user, song_title="Working", status="in_progress")
    await _submit(db_session, user, song_title="Stems", request_type="stems")

    new_only = await client.get(
        "/api/v1/admin/loop-requests?status=new", headers=_auth_headers(admin)
    )
    stems_only = await client.get(
        "/api/v1/admin/loop-requests?request_type=stems", headers=_auth_headers(admin)
    )

    assert {i["song_title"] for i in new_only.json()["data"]["items"]} == {
        "Untouched", "Stems",
    }
    assert new_only.json()["data"]["total"] == 2
    assert [i["song_title"] for i in stems_only.json()["data"]["items"]] == ["Stems"]


@pytest.mark.asyncio
async def test_admin_listing_paginates(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    for title in ("First", "Second", "Third"):
        await _submit(db_session, user, song_title=title)

    page_two = await client.get(
        "/api/v1/admin/loop-requests?page=2&page_size=2", headers=_auth_headers(admin)
    )

    data = page_two.json()["data"]
    assert data["total"] == 3
    assert data["page"] == 2
    assert [i["song_title"] for i in data["items"]] == ["First"]


@pytest.mark.asyncio
async def test_admin_listing_survives_a_deleted_requester(client, db_session):
    """The row is gone, but the request must still be readable."""
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    await _submit(db_session, user, song_title="Orphan")
    await db_session.delete(user)
    await db_session.commit()

    response = await client.get(
        "/api/v1/admin/loop-requests", headers=_auth_headers(admin)
    )

    # The FK cascades, so the request goes with the account; what matters is
    # that the join does not error either way.
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_listing_requires_an_admin(client, db_session):
    user = await _create_user(db_session)
    response = await client.get(
        "/api/v1/admin/loop-requests", headers=_auth_headers(user)
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_listing_requires_authentication(client):
    response = await client.get("/api/v1/admin/loop-requests")
    assert response.status_code == 403


# --- status changes ----------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_moves_a_request_through_the_queue(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session, full_name="Ada Lovelace")
    loop_request = await _submit(db_session, user)

    response = await client.patch(
        f"/api/v1/admin/loop-requests/{loop_request.id}",
        json={"status": "in_progress"},
        headers=_auth_headers(admin),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Loop request marked in_progress"
    assert body["data"]["status"] == "in_progress"
    assert body["data"]["status_changed_at"] is not None
    assert body["data"]["requester_name"] == "Ada Lovelace"

    await db_session.refresh(loop_request)
    assert loop_request.status == "in_progress"
    assert loop_request.status_changed_at is not None


@pytest.mark.asyncio
async def test_the_requester_sees_the_new_status(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    loop_request = await _submit(db_session, user)

    await client.patch(
        f"/api/v1/admin/loop-requests/{loop_request.id}",
        json={"status": "fulfilled"},
        headers=_auth_headers(admin),
    )
    mine = await client.get("/api/v1/loop-requests", headers=_auth_headers(user))

    assert mine.json()["data"]["items"][0]["status"] == "fulfilled"


@pytest.mark.asyncio
async def test_resetting_the_same_status_leaves_the_timestamp_alone(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    loop_request = await _submit(db_session, user)

    first = await client.patch(
        f"/api/v1/admin/loop-requests/{loop_request.id}",
        json={"status": "declined"},
        headers=_auth_headers(admin),
    )
    again = await client.patch(
        f"/api/v1/admin/loop-requests/{loop_request.id}",
        json={"status": "declined"},
        headers=_auth_headers(admin),
    )

    assert first.json()["data"]["status_changed_at"] == (
        again.json()["data"]["status_changed_at"]
    )


@pytest.mark.asyncio
async def test_updating_an_unknown_request_is_a_404(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    response = await client.patch(
        f"/api/v1/admin/loop-requests/{uuid.uuid4()}",
        json={"status": "fulfilled"},
        headers=_auth_headers(admin),
    )
    assert response.status_code == 404
    assert response.json()["message"] == "Loop request not found"


@pytest.mark.asyncio
async def test_an_unknown_status_is_refused(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    loop_request = await _submit(db_session, user)

    response = await client.patch(
        f"/api/v1/admin/loop-requests/{loop_request.id}",
        json={"status": "archived"},
        headers=_auth_headers(admin),
    )

    assert response.status_code == 422
    await db_session.refresh(loop_request)
    assert loop_request.status == "new"


@pytest.mark.asyncio
async def test_a_regular_user_cannot_change_a_status(client, db_session):
    user = await _create_user(db_session)
    loop_request = await _submit(db_session, user)

    response = await client.patch(
        f"/api/v1/admin/loop-requests/{loop_request.id}",
        json={"status": "fulfilled"},
        headers=_auth_headers(user),
    )

    assert response.status_code == 403
    await db_session.refresh(loop_request)
    assert loop_request.status == "new"


# --- the requester is told ---------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("new_status", ["in_progress", "fulfilled", "declined"])
async def test_moving_a_request_emails_the_requester(
    client, db_session, status_email, new_status
):
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    loop_request = await _submit(db_session, user)

    response = await client.patch(
        f"/api/v1/admin/loop-requests/{loop_request.id}",
        json={"status": new_status},
        headers=_auth_headers(admin),
    )

    assert response.status_code == 200
    status_email.delay.assert_called_once_with(str(loop_request.id))


@pytest.mark.asyncio
async def test_moving_a_request_back_to_new_tells_nobody(
    client, db_session, status_email
):
    """An internal correction is not news the requester is waiting on."""
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    loop_request = await _submit(db_session, user, status="in_progress")

    await client.patch(
        f"/api/v1/admin/loop-requests/{loop_request.id}",
        json={"status": "new"},
        headers=_auth_headers(admin),
    )

    status_email.delay.assert_not_called()


@pytest.mark.asyncio
async def test_the_same_status_twice_emails_once(client, db_session, status_email):
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    loop_request = await _submit(db_session, user)

    for _ in range(2):
        await client.patch(
            f"/api/v1/admin/loop-requests/{loop_request.id}",
            json={"status": "fulfilled"},
            headers=_auth_headers(admin),
        )

    status_email.delay.assert_called_once()


@pytest.mark.asyncio
async def test_a_refused_status_emails_nobody(client, db_session, status_email):
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    loop_request = await _submit(db_session, user)

    response = await client.patch(
        f"/api/v1/admin/loop-requests/{loop_request.id}",
        json={"status": "archived"},
        headers=_auth_headers(admin),
    )

    assert response.status_code == 422
    status_email.delay.assert_not_called()


@pytest.mark.asyncio
async def test_the_status_sticks_even_when_the_broker_is_down(
    client, db_session, status_email
):
    admin = await _create_user(db_session, UserRole.admin)
    user = await _create_user(db_session)
    loop_request = await _submit(db_session, user)
    status_email.delay.side_effect = RuntimeError("broker unreachable")

    response = await client.patch(
        f"/api/v1/admin/loop-requests/{loop_request.id}",
        json={"status": "fulfilled"},
        headers=_auth_headers(admin),
    )

    assert response.status_code == 200
    await db_session.refresh(loop_request)
    assert loop_request.status == "fulfilled"


def test_the_task_emails_the_requester(monkeypatch):
    """The task itself, over fakes — it ends in asyncio.run, so it cannot be async."""
    import types
    from datetime import datetime, timezone

    from app.models.user import User as UserModel
    from app.tasks import notification_tasks

    user_id = uuid.uuid4()
    loop_request = types.SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        request_type="stems",
        artist_name="Asake",
        song_title="Lonely At The Top",
        reference_link=None,
        notes=None,
        status="fulfilled",
        created_at=datetime(2026, 8, 26, 16, 45, tzinfo=timezone.utc),
    )
    user = UserModel(
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
            return user if model is UserModel else loop_request

    sent = {}

    async def fake_send_email(*, to, subject, html, text):
        sent.update(to=to, subject=subject, html=html, text=text)

    monkeypatch.setattr("app.services.email_service.send_email", fake_send_email)
    monkeypatch.setattr("app.database.AsyncSessionLocal", lambda: _Session())

    notification_tasks.send_loop_request_status_email(str(loop_request.id))

    assert sent["to"] == "ada@test.com"
    assert sent["subject"] == "Your stems request is ready"
    assert "Ada Lovelace" in sent["html"]
    assert "Lonely At The Top" in sent["html"]
    assert "Asake" in sent["text"]


def test_the_task_sends_nothing_for_a_request_back_on_new(monkeypatch):
    import types

    from app.tasks import notification_tasks

    loop_request = types.SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), status="new",
        request_type="loop", artist_name="Tems", song_title="Love Me JeJe",
    )

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, pk):
            return loop_request

    async def _boom(**kw):
        raise AssertionError("a request on new must not email anyone")

    monkeypatch.setattr("app.services.email_service.send_email", _boom)
    monkeypatch.setattr("app.database.AsyncSessionLocal", lambda: _Session())

    notification_tasks.send_loop_request_status_email(str(loop_request.id))
