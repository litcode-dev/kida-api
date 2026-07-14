import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.exceptions import AppError
from app.services.play_subscriptions import PlaySubscriptions


def _receipt(token="tok-123"):
    return json.dumps({
        "packageName": "com.litcode.kida",
        "productId": "kida.premium.monthly",
        "purchaseToken": token,
    })


def _future_rfc3339(days=30):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_verify_active_and_acknowledges_when_pending(monkeypatch):
    client = PlaySubscriptions(token_provider=AsyncMock(return_value="access"))
    data = {
        "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_PENDING",
        "lineItems": [
            {"productId": "kida.premium.monthly", "expiryTime": _future_rfc3339()},
        ],
    }
    monkeypatch.setattr(client, "get_subscription", AsyncMock(return_value=data))
    ack = AsyncMock()
    monkeypatch.setattr(client, "acknowledge", ack)

    result = await client.verify(_receipt("tok-123"))

    assert result.status == "active"
    assert result.store_transaction_id == "tok-123"
    assert result.product_id == "kida.premium.monthly"
    assert result.expires_at is not None
    ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_does_not_acknowledge_when_already_acknowledged(monkeypatch):
    client = PlaySubscriptions(token_provider=AsyncMock(return_value="access"))
    data = {
        "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
        "lineItems": [{"productId": "kida.premium.monthly", "expiryTime": _future_rfc3339()}],
    }
    monkeypatch.setattr(client, "get_subscription", AsyncMock(return_value=data))
    ack = AsyncMock()
    monkeypatch.setattr(client, "acknowledge", ack)

    await client.verify(_receipt())
    ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_maps_grace_period(monkeypatch):
    client = PlaySubscriptions(token_provider=AsyncMock(return_value="access"))
    data = {
        "subscriptionState": "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
        "lineItems": [{"productId": "kida.premium.yearly", "expiryTime": _future_rfc3339(3)}],
    }
    monkeypatch.setattr(client, "get_subscription", AsyncMock(return_value=data))
    result = await client.verify(_receipt())
    assert result.status == "grace"


@pytest.mark.asyncio
async def test_verify_rejects_bad_json():
    client = PlaySubscriptions(token_provider=AsyncMock(return_value="access"))
    with pytest.raises(AppError):
        await client.verify("not-json")


@pytest.mark.asyncio
async def test_verify_rejects_missing_token():
    client = PlaySubscriptions(token_provider=AsyncMock(return_value="access"))
    with pytest.raises(AppError):
        await client.verify(json.dumps({"packageName": "com.litcode.kida", "productId": "x"}))
