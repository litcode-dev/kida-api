import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import onesignal_service
from app.services.onesignal_service import send_to_external_ids, send_to_user


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


def _mock_client_seq(*responses):
    """Like _mock_client, but post() answers each call with the next response."""
    client = _mock_client(200)
    replies = []
    for status_code, body in responses:
        reply = MagicMock()
        reply.status_code = status_code
        reply.json = MagicMock(return_value=body)
        replies.append(reply)
    client.post = AsyncMock(side_effect=replies)
    return client


@pytest.mark.asyncio
async def test_send_to_user_targets_the_external_id():
    client = _mock_client(200, {"id": "notif-1"})

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        result = await send_to_user("user-1", "Title", "Body", data={"k": "v"})

    call = client.post.call_args
    assert call.args[0] == "https://api.onesignal.com/notifications"
    assert call.kwargs["headers"]["Authorization"].startswith("Key ")
    payload = call.kwargs["json"]
    assert payload["include_aliases"] == {"external_id": ["user-1"]}
    assert payload["target_channel"] == "push"
    assert "include_player_ids" not in payload
    assert payload["headings"] == {"en": "Title"}
    assert payload["contents"] == {"en": "Body"}
    assert payload["data"] == {"k": "v"}
    assert result["unreachable"] == []
    assert result["by_external_id"] == 1
    assert result["responses"] == [{"status_code": 200, "body": {"id": "notif-1"}}]


@pytest.mark.asyncio
async def test_unknown_external_id_falls_back_to_the_registered_device():
    client = _mock_client_seq(
        (200, {"id": "", "errors": ["All included players are not subscribed"]}),
        (200, {"id": "notif-2"}),
    )

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        result = await send_to_user("user-1", "Title", "Body", subscription_id="sub-1")

    second = client.post.call_args_list[1].kwargs["json"]
    assert second["include_subscription_ids"] == ["sub-1"]
    assert result["unreachable"] == []
    assert result["by_external_id"] == 0
    assert result["by_subscription_id"] == 1


@pytest.mark.asyncio
async def test_unknown_external_id_without_a_device_is_unreachable():
    client = _mock_client(200, {"id": "", "errors": ["All included players are not subscribed"]})

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        result = await send_to_user("user-1", "Title", "Body")

    assert client.post.call_count == 1
    assert result["unreachable"] == ["user-1"]


@pytest.mark.asyncio
async def test_partial_miss_only_retries_the_unknown_users():
    client = _mock_client_seq(
        (200, {"id": "notif-1", "errors": {"invalid_aliases": {"external_id": ["u2", "u3"]}}}),
        (200, {"id": "notif-2"}),
    )

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        result = await send_to_external_ids(
            ["u1", "u2", "u3"], "Title", "Body",
            fallback_subscription_ids={"u1": "s1", "u2": "s2"},
        )

    second = client.post.call_args_list[1].kwargs["json"]
    assert second["include_subscription_ids"] == ["s2"]
    assert result["by_external_id"] == 1
    assert result["by_subscription_id"] == 1
    assert result["unreachable"] == ["u3"]


@pytest.mark.asyncio
async def test_a_failed_fallback_leaves_the_user_unreachable():
    client = _mock_client_seq(
        (200, {"id": "", "errors": ["All included players are not subscribed"]}),
        (400, {"errors": ["Invalid subscription id"]}),
    )

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        result = await send_to_user("user-1", "Title", "Body", subscription_id="sub-1")

    assert result["by_subscription_id"] == 0
    assert result["unreachable"] == ["user-1"]


@pytest.mark.asyncio
async def test_send_tolerates_a_non_json_body():
    client = _mock_client(502)
    client.post.return_value.json = MagicMock(side_effect=ValueError("no json"))

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        result = await send_to_user("user-1", "Title", "Body")

    assert result["responses"] == [{"status_code": 502, "body": {}}]
    assert result["unreachable"] == ["user-1"]


@pytest.mark.asyncio
async def test_large_audiences_are_sent_in_batches(monkeypatch):
    monkeypatch.setattr(onesignal_service, "SEND_BATCH", 2)
    client = _mock_client_seq((200, {"id": "a"}), (200, {"id": "b"}))

    with patch("app.services.onesignal_service.httpx.AsyncClient", return_value=client):
        result = await send_to_external_ids(["u1", "u2", "u3"], "Title", "Body")

    batches = [c.kwargs["json"]["include_aliases"]["external_id"] for c in client.post.call_args_list]
    assert batches == [["u1", "u2"], ["u3"]]
    assert result["by_external_id"] == 3


def _settings(app_id: str, api_key: str):
    return SimpleNamespace(onesignal_app_id=app_id, onesignal_api_key=api_key)


def test_delivery_problem_is_none_when_both_credentials_are_set():
    assert onesignal_service.delivery_problem(_settings("app", "key")) is None


def test_delivery_problem_names_every_missing_credential():
    """The digest records this on its run row, so it has to say which one."""
    problem = onesignal_service.delivery_problem(_settings("", ""))

    assert "ONESIGNAL_APP_ID" in problem
    assert "ONESIGNAL_API_KEY" in problem
