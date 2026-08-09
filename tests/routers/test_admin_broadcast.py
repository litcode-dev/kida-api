import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.newsletter import NewsletterSubscriber
from app.models.user import User, UserRole
from app.schemas.broadcast import BroadcastAudience
from app.services import broadcast_service
from app.services.auth_service import create_access_token, hash_password

BROADCAST = "/api/v1/admin/email/broadcast"


async def _make_user(db, email=None, role=UserRole.user):
    user = User(
        id=uuid.uuid4(),
        email=email or f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Test User",
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


async def _subscribe(db, email, active=True):
    db.add(NewsletterSubscriber(
        email=email,
        is_active=active,
        unsubscribed_at=None if active else datetime.now(timezone.utc),
    ))
    await db.commit()


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


def _payload(**over):
    body = {"subject": "New drop", "body": "We shipped something."}
    body.update(over)
    return body


# ── Audience resolution ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_users_audience_is_registered_accounts(db_session):
    await _make_user(db_session, email="a@test.com")
    await _make_user(db_session, email="b@test.com")
    await _subscribe(db_session, "newsletter-only@test.com")

    emails = await broadcast_service.resolve_recipients(db_session, BroadcastAudience.users)

    assert emails == ["a@test.com", "b@test.com"]


@pytest.mark.asyncio
async def test_subscribers_audience_is_active_newsletter_only(db_session):
    await _make_user(db_session, email="account@test.com")
    await _subscribe(db_session, "active@test.com")
    await _subscribe(db_session, "gone@test.com", active=False)

    emails = await broadcast_service.resolve_recipients(db_session, BroadcastAudience.subscribers)

    assert emails == ["active@test.com"]


@pytest.mark.asyncio
async def test_all_audience_is_the_deduplicated_union(db_session):
    await _make_user(db_session, email="both@test.com")
    await _subscribe(db_session, "both@test.com")
    await _make_user(db_session, email="account@test.com")
    await _subscribe(db_session, "subscriber@test.com")

    emails = await broadcast_service.resolve_recipients(db_session, BroadcastAudience.all)

    assert emails == ["account@test.com", "both@test.com", "subscriber@test.com"]


@pytest.mark.asyncio
async def test_unsubscribed_user_is_excluded_even_from_the_users_audience(db_session):
    """Holding an account is not consent to marketing."""
    await _make_user(db_session, email="optout@test.com")
    await _make_user(db_session, email="fine@test.com")
    await _subscribe(db_session, "optout@test.com", active=False)

    for audience in BroadcastAudience:
        emails = await broadcast_service.resolve_recipients(db_session, audience)
        assert "optout@test.com" not in emails

    assert await broadcast_service.resolve_recipients(
        db_session, BroadcastAudience.users
    ) == ["fine@test.com"]


@pytest.mark.asyncio
async def test_addresses_are_matched_case_insensitively(db_session):
    await _make_user(db_session, email="Mixed@Test.com")
    await _subscribe(db_session, "mixed@test.com", active=False)

    emails = await broadcast_service.resolve_recipients(db_session, BroadcastAudience.all)

    assert emails == []


# ── Access control ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_broadcast_requires_authentication(client):
    resp = await client.post(BROADCAST, json=_payload())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_cannot_broadcast(client, db_session):
    caller = await _make_user(db_session, role=UserRole.producer)
    resp = await client.post(BROADCAST, json=_payload(), headers=_headers(caller))
    assert resp.status_code == 403


# ── Sending ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_broadcast_queues_the_task_with_the_payload(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    await _make_user(db_session, email="reader@test.com")

    with patch("app.routers.admin.send_broadcast_email") as task:
        resp = await client.post(
            BROADCAST,
            json=_payload(heading="Big news", cta_label="Browse", cta_url="https://kida.example"),
            headers=_headers(admin),
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["recipients"] == 2  # admin + reader
    task.delay.assert_called_once_with(
        "New drop", "We shipped something.", "all", "Big news", "Browse", "https://kida.example",
    )


@pytest.mark.asyncio
async def test_broadcast_with_no_recipients_is_rejected(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    await _subscribe(db_session, admin.email, active=False)

    with patch("app.routers.admin.send_broadcast_email") as task:
        resp = await client.post(
            BROADCAST, json=_payload(audience="all"), headers=_headers(admin)
        )

    assert resp.status_code == 422
    task.delay.assert_not_called()


@pytest.mark.asyncio
async def test_cta_label_without_url_is_rejected(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    resp = await client.post(
        BROADCAST, json=_payload(cta_label="Browse"), headers=_headers(admin)
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_subject_is_rejected(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    resp = await client.post(BROADCAST, json=_payload(subject=""), headers=_headers(admin))
    assert resp.status_code == 422


# ── Recipient count and preview ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recipient_count_sends_nothing(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    await _make_user(db_session, email="reader@test.com")

    with patch("app.services.email_service.send_email", new=AsyncMock()) as sender:
        resp = await client.get(
            "/api/v1/admin/email/broadcast/recipients?audience=users",
            headers=_headers(admin),
        )

    assert resp.status_code == 200
    assert resp.json()["data"] == {"audience": "users", "recipients": 2}
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_sends_only_to_the_given_address(client, db_session):
    admin = await _make_user(db_session, role=UserRole.admin)
    await _make_user(db_session, email="reader@test.com")

    sent = []

    async def _capture(**kw):
        sent.append(kw)

    with patch("app.services.email_service.send_email", new=_capture), \
         patch("app.routers.admin.send_broadcast_email") as task:
        resp = await client.post(
            "/api/v1/admin/email/broadcast/preview",
            json=_payload(to="check@test.com"),
            headers=_headers(admin),
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["would_reach"] == 2
    assert [s["to"] for s in sent] == ["check@test.com"]
    assert sent[0]["subject"].startswith("[Preview]")
    task.delay.assert_not_called()


# ── The task itself ──────────────────────────────────────────────────────────
#
# Plain sync tests: the Celery task calls asyncio.run, which cannot nest inside
# the loop pytest-asyncio runs async tests on.

def _run_task(monkeypatch, recipients):
    from app.tasks import notification_tasks

    monkeypatch.setattr(
        "app.services.broadcast_service.resolve_recipients",
        AsyncMock(return_value=recipients),
    )
    sent = []

    async def _capture(**kw):
        sent.append(kw)

    monkeypatch.setattr("app.services.email_service.send_email", _capture)
    # The task opens its own session; nothing here should reach a database.
    monkeypatch.setattr("app.database.AsyncSessionLocal", MagicMock(return_value=AsyncMock()))

    notification_tasks.send_broadcast_email("Subject", "Body line.", "all", "Heading")
    return sent


def test_task_sends_to_every_resolved_recipient(monkeypatch):
    recipients = ["a@test.com", "b@test.com", "c@test.com"]
    sent = _run_task(monkeypatch, recipients)

    assert [s["to"] for s in sent] == recipients
    assert {s["subject"] for s in sent} == {"Subject"}
    # The rendered body is identical for everyone — built once, not per address.
    assert len({s["html"] for s in sent}) == 1
    assert "Body line." in sent[0]["text"]
    assert "Heading" in sent[0]["html"]


def test_task_with_no_recipients_sends_nothing(monkeypatch):
    assert _run_task(monkeypatch, []) == []
