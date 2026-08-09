import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.onesignal_service import send_notification


def _mock_client(status_code: int, body: dict | None = None):
    """httpx.AsyncClient stand-in whose post() returns a canned response.

    The response itself is a MagicMock, not an AsyncMock — resp.json() is
    called synchronously by _post, so an AsyncMock would hand back a coroutine.
    """
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=body if body is not None else {})

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_send_notification_returns_status_and_body_on_success():
    client = _mock_client(200, {"id": "notif-1", "recipients": 1})

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        result = await send_notification("player-1", "Title", "Body")

    assert result == {"status_code": 200, "body": {"id": "notif-1", "recipients": 1}}


@pytest.mark.asyncio
async def test_send_notification_reports_the_error_status():
    client = _mock_client(400, {"errors": ["Invalid player id"]})

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        result = await send_notification("player-1", "Title", "Body")

    assert result["status_code"] == 400
    assert result["body"] == {"errors": ["Invalid player id"]}


@pytest.mark.asyncio
async def test_send_notification_tolerates_a_non_json_body():
    client = _mock_client(502)
    client.post.return_value.json = MagicMock(side_effect=ValueError("no json"))

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        result = await send_notification("player-1", "Title", "Body")

    assert result == {"status_code": 502, "body": {}}


@pytest.mark.asyncio
async def test_send_notification_posts_the_expected_payload():
    client = _mock_client(200)

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        await send_notification("player-1", "Title", "Body", data={"k": "v"})

    payload = client.post.call_args.kwargs["json"]
    assert payload["include_player_ids"] == ["player-1"]
    assert payload["headings"] == {"en": "Title"}
    assert payload["contents"] == {"en": "Body"}
    assert payload["data"] == {"k": "v"}
