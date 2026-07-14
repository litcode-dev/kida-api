from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.exceptions import AppError
from app.services.app_store_subscriptions import AppStoreSubscriptions


def _future_ms(days=30):
    return str(int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp() * 1000))


def _past_ms(days=1):
    return str(int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000))


@pytest.mark.asyncio
async def test_verify_active_reads_latest_transaction(monkeypatch):
    client = AppStoreSubscriptions(shared_secret="secret")
    resp = {
        "status": 0,
        "latest_receipt": "new-b64-receipt",
        "latest_receipt_info": [
            {"original_transaction_id": "orig-1", "product_id": "kida.premium.monthly",
             "expires_date_ms": _past_ms(40), "purchase_date_ms": _past_ms(70)},
            {"original_transaction_id": "orig-1", "product_id": "kida.premium.yearly",
             "expires_date_ms": _future_ms(365), "purchase_date_ms": _past_ms(1)},
        ],
    }
    monkeypatch.setattr(client, "_post", AsyncMock(return_value=resp))

    result = await client.verify("b64")
    assert result.status == "active"
    assert result.store_transaction_id == "orig-1"
    assert result.product_id == "kida.premium.yearly"  # latest by expiry
    assert result.latest_receipt == "new-b64-receipt"


@pytest.mark.asyncio
async def test_verify_expired(monkeypatch):
    client = AppStoreSubscriptions(shared_secret="secret")
    resp = {
        "status": 0,
        "latest_receipt_info": [
            {"original_transaction_id": "orig-2", "product_id": "kida.premium.monthly",
             "expires_date_ms": _past_ms(2)},
        ],
    }
    monkeypatch.setattr(client, "_post", AsyncMock(return_value=resp))
    result = await client.verify("b64")
    assert result.status == "expired"
    assert result.store_transaction_id == "orig-2"


@pytest.mark.asyncio
async def test_verify_grace_period(monkeypatch):
    client = AppStoreSubscriptions(shared_secret="secret")
    resp = {
        "status": 0,
        "latest_receipt_info": [
            {"original_transaction_id": "orig-3", "product_id": "kida.premium.monthly",
             "expires_date_ms": _past_ms(1)},
        ],
        "pending_renewal_info": [
            {"original_transaction_id": "orig-3", "grace_period_expires_date_ms": _future_ms(5)},
        ],
    }
    monkeypatch.setattr(client, "_post", AsyncMock(return_value=resp))
    result = await client.verify("b64")
    assert result.status == "grace"
    assert result.expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_verify_invalid_status_raises(monkeypatch):
    client = AppStoreSubscriptions(shared_secret="secret")
    monkeypatch.setattr(client, "_post", AsyncMock(return_value={"status": 21002}))
    with pytest.raises(AppError):
        await client.verify("b64")


@pytest.mark.asyncio
async def test_verify_retries_sandbox_on_21007(monkeypatch):
    client = AppStoreSubscriptions(shared_secret="secret")
    good = {
        "status": 0,
        "latest_receipt_info": [
            {"original_transaction_id": "orig-4", "product_id": "kida.premium.monthly",
             "expires_date_ms": _future_ms()},
        ],
    }
    post = AsyncMock(side_effect=[{"status": 21007}, good])
    monkeypatch.setattr(client, "_post", post)
    result = await client.verify("b64")
    assert result.store_transaction_id == "orig-4"
    assert post.await_count == 2  # production, then sandbox
