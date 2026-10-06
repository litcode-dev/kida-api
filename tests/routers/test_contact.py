import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_task(monkeypatch):
    import app.routers.contact as mod
    task = MagicMock()
    monkeypatch.setattr(mod, "send_contact_admin_notification", task)
    return task


@pytest.mark.asyncio
async def test_contact_enqueues_admin_email(client, mock_task):
    resp = await client.post(
        "/api/v1/contact",
        json={
            "name": "  Ada  ",
            "email": "ada@test.com",
            "subject": "Licensing",
            "message": "  Can I use a loop in a film?  ",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["data"]["email"] == "ada@test.com"
    mock_task.delay.assert_called_once_with(
        name="Ada",
        email="ada@test.com",
        subject="Licensing",
        message="Can I use a loop in a film?",
    )


@pytest.mark.asyncio
async def test_contact_subject_is_optional(client, mock_task):
    resp = await client.post(
        "/api/v1/contact",
        json={"name": "Ada", "email": "ada@test.com", "subject": "  ", "message": "Hi"},
    )
    assert resp.status_code == 200
    assert mock_task.delay.call_args.kwargs["subject"] is None


@pytest.mark.asyncio
async def test_contact_collapses_newlines_in_header_fields(client, mock_task):
    resp = await client.post(
        "/api/v1/contact",
        json={
            "name": "Ada\r\nBcc: victim@test.com",
            "email": "ada@test.com",
            "subject": "Hi\nX-Injected: yes",
            "message": "Line one\nLine two",
        },
    )
    assert resp.status_code == 200
    kwargs = mock_task.delay.call_args.kwargs
    assert kwargs["name"] == "Ada Bcc: victim@test.com"
    assert kwargs["subject"] == "Hi X-Injected: yes"
    assert kwargs["message"] == "Line one\nLine two"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"email": "ada@test.com", "message": "Hi"},
        {"name": "Ada", "message": "Hi"},
        {"name": "Ada", "email": "not-an-email", "message": "Hi"},
        {"name": "Ada", "email": "ada@test.com"},
        {"name": "Ada", "email": "ada@test.com", "message": "   "},
        {"name": "Ada", "email": "ada@test.com", "message": "x" * 5_001},
    ],
)
async def test_contact_rejects_invalid_payload(client, mock_task, payload):
    resp = await client.post("/api/v1/contact", json=payload)
    assert resp.status_code == 422
    mock_task.delay.assert_not_called()


@pytest.mark.asyncio
async def test_contact_rejects_offensive_message(client, mock_task):
    from app.utils.text_moderation import find_banned_term
    assert find_banned_term("fuck") is not None

    resp = await client.post(
        "/api/v1/contact",
        json={"name": "Ada", "email": "ada@test.com", "message": "fuck this"},
    )
    assert resp.status_code == 422
    mock_task.delay.assert_not_called()


def test_admin_notification_replies_to_sender(monkeypatch):
    from app.config import get_settings
    from app.services import email_service
    from app.tasks.notification_tasks import send_contact_admin_notification

    monkeypatch.setattr(get_settings(), "admin_notification_email", "team@test.com")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(email_service, "send_email", send)

    send_contact_admin_notification(
        name="Ada", email="ada@test.com", subject=None, message="<b>Hi</b>",
    )

    kwargs = send.call_args.kwargs
    assert kwargs["to"] == "team@test.com"
    assert kwargs["reply_to"] == "ada@test.com"
    assert kwargs["subject"] == "Contact from Ada"
    assert "&lt;b&gt;Hi&lt;/b&gt;" in kwargs["html"]
    assert "<b>Hi</b>" in kwargs["text"]


def test_admin_notification_skipped_without_recipient(monkeypatch):
    from app.config import get_settings
    from app.services import email_service
    from app.tasks.notification_tasks import send_contact_admin_notification

    monkeypatch.setattr(get_settings(), "admin_notification_email", "")
    send = AsyncMock()
    monkeypatch.setattr(email_service, "send_email", send)

    send_contact_admin_notification(
        name="Ada", email="ada@test.com", subject="Hi", message="Hi",
    )
    send.assert_not_called()
