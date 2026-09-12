import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models.iap_subscription import (
    IapPlatform,
    IapSubscription,
    IapSubscriptionSource,
    IapSubscriptionStatus,
)
from app.models.loop_request import LoopRequest
from app.models.user import User, UserRole
from app.services import loop_request_quota_service
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
    assert body["data"]["status"] == "new"

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
    assert sent["subject"] == "Stems request: Love Me JeJe by Tems"
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


@pytest.mark.asyncio
async def test_a_user_sees_only_their_own_requests(client, db_session):
    mine = await _create_user(db_session)
    someone_else = await _create_user(db_session)
    await _submit(db_session, mine, song_title="Mine")
    await _submit(db_session, someone_else, song_title="Theirs")

    response = await client.get("/api/v1/loop-requests", headers=_auth_headers(mine))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1
    assert [item["song_title"] for item in data["items"]] == ["Mine"]


@pytest.mark.asyncio
async def test_own_requests_are_newest_first_and_paginated(client, db_session):
    user = await _create_user(db_session)
    for title in ("First", "Second", "Third"):
        await _submit(db_session, user, song_title=title)

    first_page = await client.get(
        "/api/v1/loop-requests?page=1&page_size=2", headers=_auth_headers(user)
    )
    second_page = await client.get(
        "/api/v1/loop-requests?page=2&page_size=2", headers=_auth_headers(user)
    )

    assert [i["song_title"] for i in first_page.json()["data"]["items"]] == [
        "Third", "Second",
    ]
    assert [i["song_title"] for i in second_page.json()["data"]["items"]] == ["First"]
    assert first_page.json()["data"]["total"] == 3


@pytest.mark.asyncio
async def test_own_requests_filter_by_type_and_status(client, db_session):
    user = await _create_user(db_session)
    await _submit(db_session, user, request_type="loop", song_title="A loop")
    await _submit(db_session, user, request_type="stems", song_title="Some stems")
    await _submit(
        db_session, user, request_type="stems", song_title="Done stems",
        status="fulfilled",
    )

    by_type = await client.get(
        "/api/v1/loop-requests?request_type=stems", headers=_auth_headers(user)
    )
    by_status = await client.get(
        "/api/v1/loop-requests?status=fulfilled", headers=_auth_headers(user)
    )

    assert {i["song_title"] for i in by_type.json()["data"]["items"]} == {
        "Some stems", "Done stems",
    }
    assert [i["song_title"] for i in by_status.json()["data"]["items"]] == ["Done stems"]


@pytest.mark.asyncio
async def test_listing_own_requests_requires_authentication(client):
    response = await client.get("/api/v1/loop-requests")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_listing_own_requests_rejects_an_unknown_status(client, db_session):
    user = await _create_user(db_session)
    response = await client.get(
        "/api/v1/loop-requests?status=archived", headers=_auth_headers(user)
    )
    assert response.status_code == 422


# --- moderation --------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("artist_name", "some retard"),
        ("song_title", "n1gger anthem"),
        ("notes", "make it fast you retard"),
        ("notes", "hurry up you prick"),
    ],
)
async def test_offensive_text_is_refused(client, db_session, field, value):
    user = await _create_user(db_session)
    body = {
        "request_type": "loop",
        "artist_name": "Tems",
        "song_title": "Love Me JeJe",
    }
    body[field] = value

    response = await client.post(
        "/api/v1/loop-requests", json=body, headers=_auth_headers(user)
    )

    assert response.status_code == 422
    message = response.json()["message"]
    assert field in message
    assert "offensive language" in message
    assert await db_session.scalar(select(LoopRequest)) is None


@pytest.mark.asyncio
async def test_the_refusal_names_the_offending_word(client, db_session):
    """Four text fields go in; the submitter has to know which one to fix."""
    user = await _create_user(db_session)

    response = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "loop",
            "artist_name": "Tems",
            "song_title": "Love Me JeJe",
            "notes": "what the f*ck",
        },
        headers=_auth_headers(user),
    )

    assert response.status_code == 422
    assert "f*ck" in response.json()["message"]


@pytest.mark.asyncio
async def test_an_offensive_reference_link_is_refused(client, db_session):
    user = await _create_user(db_session)

    response = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "loop",
            "artist_name": "Tems",
            "song_title": "Love Me JeJe",
            "reference_link": "https://example.com/fuck-you",
        },
        headers=_auth_headers(user),
    )

    assert response.status_code == 422
    assert "reference_link" in response.json()["message"]


@pytest.mark.asyncio
async def test_ordinary_words_containing_a_banned_run_still_go_through(
    client, db_session
):
    """A filter that rejects "classic" is worse than no filter at all."""
    user = await _create_user(db_session)

    response = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "stems",
            "artist_name": "Cockburn",
            "song_title": "Classic Bassline",
            "notes": "Assess the mix — cocktail-hour energy, Scunthorpe session.",
        },
        headers=_auth_headers(user),
    )

    assert response.status_code == 201
    assert response.json()["data"]["song_title"] == "Classic Bassline"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("artist_name", "Dick Dale"),
        ("song_title", "Fuck tha Police"),
        ("song_title", "Bitch Better Have My Money"),
    ],
)
async def test_a_real_release_is_not_refused_for_its_own_title(
    client, db_session, field, value
):
    """These are citations, not the submitter's words — there is no other
    spelling, so refusing them refuses the request rather than the behaviour."""
    user = await _create_user(db_session)
    body = {
        "request_type": "loop",
        "artist_name": "Tems",
        "song_title": "Love Me JeJe",
    }
    body[field] = value

    response = await client.post(
        "/api/v1/loop-requests", json=body, headers=_auth_headers(user)
    )

    assert response.status_code == 201
    assert response.json()["data"][field] == value


@pytest.mark.asyncio
async def test_a_slur_in_a_title_is_still_refused(client, db_session):
    """No catalogue lookup needs one."""
    user = await _create_user(db_session)

    response = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "loop",
            "artist_name": "Tems",
            "song_title": "faggot anthem",
        },
        headers=_auth_headers(user),
    )

    assert response.status_code == 422
    assert "song_title" in response.json()["message"]
    assert await db_session.scalar(select(LoopRequest)) is None


# --- The monthly request allowance -------------------------------------------
#
# One request a month on the free tier, unlimited on Kiɗa Premium. Loops and
# stems are counted together, since both land in the same hand-worked inbox.


async def _subscribe(db, user_id, status=IapSubscriptionStatus.active, expires_in_days=30):
    sub = IapSubscription(
        user_id=user_id,
        platform=IapPlatform.android,
        product_id="kida.premium.monthly",
        store_transaction_id=str(uuid.uuid4()),
        status=status,
        source=IapSubscriptionSource.store,
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
    )
    db.add(sub)
    await db.commit()
    return sub


async def _post_request(client, user, song_title="Love Me JeJe", request_type="loop"):
    return await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": request_type,
            "artist_name": "Tems",
            "song_title": song_title,
        },
        headers=_auth_headers(user),
    )


async def _request_count(db, user_id):
    return await db.scalar(
        select(func.count()).select_from(LoopRequest).where(LoopRequest.user_id == user_id)
    )


@pytest.mark.asyncio
async def test_a_free_account_gets_one_request_a_month(client, db_session, notify_task):
    user = await _create_user(db_session)

    first = await _post_request(client, user, song_title="Love Me JeJe")
    second = await _post_request(client, user, song_title="Higher")

    assert first.status_code == 201
    assert second.status_code == 403
    body = second.json()
    assert body["error"] == "loop_request_limit"
    assert body["limit"] == 1
    assert body["resets_at"] == loop_request_quota_service.next_period_start().isoformat()

    # The refused one is neither stored nor put in front of the team.
    assert await _request_count(db_session, user.id) == 1
    notify_task.delay.assert_called_once()


@pytest.mark.asyncio
async def test_stems_and_loops_share_the_one_allowance(client, db_session):
    """Both end up in the same inbox, so asking for stems spends the month too."""
    user = await _create_user(db_session)

    first = await _post_request(client, user, request_type="stems")
    second = await _post_request(client, user, request_type="loop", song_title="Higher")

    assert first.status_code == 201
    assert second.status_code == 403


@pytest.mark.asyncio
async def test_a_subscriber_may_keep_requesting(client, db_session):
    user = await _create_user(db_session)
    await _subscribe(db_session, user.id)

    for title in ("One", "Two", "Three"):
        response = await _post_request(client, user, song_title=title)
        assert response.status_code == 201

    assert await _request_count(db_session, user.id) == 3


@pytest.mark.asyncio
async def test_the_billing_grace_period_still_counts_as_premium(client, db_session):
    user = await _create_user(db_session)
    await _subscribe(db_session, user.id, status=IapSubscriptionStatus.grace)

    assert (await _post_request(client, user, song_title="One")).status_code == 201
    assert (await _post_request(client, user, song_title="Two")).status_code == 201


@pytest.mark.asyncio
async def test_an_expired_subscription_does_not_lift_the_cap(client, db_session):
    user = await _create_user(db_session)
    await _subscribe(
        db_session, user.id, status=IapSubscriptionStatus.expired, expires_in_days=-1
    )

    assert (await _post_request(client, user, song_title="One")).status_code == 201
    assert (await _post_request(client, user, song_title="Two")).status_code == 403


@pytest.mark.asyncio
async def test_last_months_request_does_not_count(client, db_session):
    """The allowance resets with the calendar month — nothing is swept."""
    user = await _create_user(db_session)
    await _submit(
        db_session,
        user,
        song_title="Last month",
        created_at=loop_request_quota_service.period_start() - timedelta(days=1),
    )

    response = await _post_request(client, user, song_title="This month")

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_a_declined_request_still_spends_the_month(client, db_session):
    """It was read and answered by hand, which is what the cap is protecting."""
    user = await _create_user(db_session)
    await _submit(db_session, user, song_title="Declined", status="declined")

    response = await _post_request(client, user, song_title="Another")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_one_users_request_does_not_spend_anothers(client, db_session):
    user = await _create_user(db_session)
    someone_else = await _create_user(db_session)
    await _submit(db_session, someone_else, song_title="Theirs")

    assert (await _post_request(client, user)).status_code == 201


@pytest.mark.asyncio
async def test_quota_reports_what_is_left_this_month(client, db_session):
    user = await _create_user(db_session)

    before = await client.get("/api/v1/loop-requests/quota", headers=_auth_headers(user))
    await _post_request(client, user)
    after = await client.get("/api/v1/loop-requests/quota", headers=_auth_headers(user))

    assert before.status_code == 200
    fresh = before.json()["data"]
    assert fresh["used"] == 0
    assert fresh["limit"] == 1
    assert fresh["remaining"] == 1
    assert fresh["unlimited"] is False
    assert fresh["premium"] is False
    assert fresh["period"] == loop_request_quota_service.current_period()
    assert fresh["resets_at"] == loop_request_quota_service.next_period_start().isoformat()

    spent = after.json()["data"]
    assert spent["used"] == 1
    assert spent["remaining"] == 0


@pytest.mark.asyncio
async def test_quota_reads_unlimited_for_a_subscriber(client, db_session):
    user = await _create_user(db_session)
    await _subscribe(db_session, user.id)
    await _post_request(client, user)

    response = await client.get("/api/v1/loop-requests/quota", headers=_auth_headers(user))

    data = response.json()["data"]
    assert data["limit"] == "unlimited"
    assert data["remaining"] == "unlimited"
    assert data["unlimited"] is True
    assert data["premium"] is True
    # Still counted, so the app can show what was asked for this month.
    assert data["used"] == 1


@pytest.mark.asyncio
async def test_quota_requires_authentication(client):
    response = await client.get("/api/v1/loop-requests/quota")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_cap_can_be_turned_off_for_everybody(client, db_session, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("FREE_TIER_MONTHLY_LOOP_REQUESTS", "unlimited")
    try:
        user = await _create_user(db_session)
        assert (await _post_request(client, user, song_title="One")).status_code == 201
        assert (await _post_request(client, user, song_title="Two")).status_code == 201

        quota = await client.get(
            "/api/v1/loop-requests/quota", headers=_auth_headers(user)
        )
        assert quota.json()["data"]["unlimited"] is True
        assert quota.json()["data"]["premium"] is False
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_zero_closes_the_form_to_free_accounts(client, db_session, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("FREE_TIER_MONTHLY_LOOP_REQUESTS", "0")
    try:
        user = await _create_user(db_session)
        response = await _post_request(client, user)
        assert response.status_code == 403
        assert response.json()["limit"] == 0
    finally:
        get_settings.cache_clear()
