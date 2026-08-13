"""Bulk sending is where the digest's cost saving actually lands.

One request per 100 addresses instead of one per address, and a failing chunk
must not take the rest of the send down with it.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import email_service


@pytest.fixture
def resend_settings():
    settings = email_service.get_settings()
    with patch.object(email_service, "get_settings") as get:
        get.return_value = type(
            "S", (), {
                "email_backend": "resend",
                "resend_api_key": "test-key",
                "resend_from": "Kida <noreply@test>",
                "email_batch_size": settings.email_batch_size,
            },
        )()
        yield get.return_value


@pytest.mark.asyncio
async def test_no_recipients_sends_nothing(resend_settings):
    with patch.object(email_service, "_send_batch_via_resend", new=AsyncMock()) as batch:
        sent, failed = await email_service.send_bulk_email([], "s", "<p>h</p>", "t")

    assert (sent, failed) == (0, 0)
    batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipients_are_chunked_to_the_batch_size(resend_settings):
    resend_settings.email_batch_size = 100
    recipients = [f"user{i}@test.com" for i in range(250)]

    with patch.object(email_service, "_send_batch_via_resend", new=AsyncMock()) as batch:
        sent, failed = await email_service.send_bulk_email(
            recipients, "subject", "<p>html</p>", "text"
        )

    assert (sent, failed) == (250, 0)
    # 250 addresses in three requests, not 250.
    assert batch.await_count == 3
    assert [len(call.args[1]) for call in batch.await_args_list] == [100, 100, 50]


@pytest.mark.asyncio
async def test_a_failing_chunk_does_not_stop_the_rest(resend_settings):
    resend_settings.email_batch_size = 2
    recipients = ["a@test.com", "b@test.com", "c@test.com", "d@test.com"]

    calls = {"n": 0}

    async def _flaky(settings, chunk, subject, html, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("provider down")

    with patch.object(email_service, "_send_batch_via_resend", new=_flaky):
        sent, failed = await email_service.send_bulk_email(
            recipients, "subject", "<p>html</p>", "text"
        )

    assert sent == 2
    assert failed == 2
