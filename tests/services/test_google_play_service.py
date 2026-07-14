from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.services import google_play_service


def _resp(status_code: int):
    resp = AsyncMock()
    resp.status_code = status_code
    resp.raise_for_status = lambda: None
    return resp


def _mock_client():
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def test_build_product_body_managed_non_consumable():
    body = google_play_service.build_product_body(
        "kida.loop.test_abcd1234", "My Loop", "A great loop", Decimal("2.99")
    )
    assert body["purchaseType"] == "managedUser"
    assert body["status"] == "active"
    assert body["sku"] == "kida.loop.test_abcd1234"
    assert body["defaultPrice"] == {"priceMicros": "2990000", "currency": "USD"}
    # NG region pinned explicitly
    assert "NG" in body["prices"]
    assert body["prices"]["NG"]["currency"] == "NGN"


@pytest.mark.asyncio
async def test_upsert_product_inserts_when_new():
    client = _mock_client()
    client.post = AsyncMock(return_value=_resp(200))
    client.put = AsyncMock()

    with patch("app.services.google_play_service.httpx.AsyncClient", return_value=client), \
         patch("app.services.google_play_service._get_access_token", AsyncMock(return_value="tok")):
        await google_play_service.upsert_product(
            "kida.loop.test_abcd1234", "My Loop", "desc", Decimal("1.99")
        )

    client.post.assert_awaited_once()
    client.put.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_product_updates_on_409():
    client = _mock_client()
    client.post = AsyncMock(return_value=_resp(409))  # SKU already exists
    client.put = AsyncMock(return_value=_resp(200))

    with patch("app.services.google_play_service.httpx.AsyncClient", return_value=client), \
         patch("app.services.google_play_service._get_access_token", AsyncMock(return_value="tok")):
        await google_play_service.upsert_product(
            "kida.loop.test_abcd1234", "My Loop", "desc", Decimal("1.99")
        )

    client.post.assert_awaited_once()
    client.put.assert_awaited_once()  # fell through to update
    assert "kida.loop.test_abcd1234" in client.put.call_args.args[0]
