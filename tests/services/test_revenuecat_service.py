from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.config import get_settings
from app.exceptions import AppError
from app.models.iap_subscription import IapPlatform, IapSubscriptionStatus
from app.services import revenuecat_service
from app.services.revenuecat_service import RevenueCatClient


def _future_ms(days=30):
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp() * 1000)


def _past_ms(days=1):
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)


def _future_iso(days=30):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _past_iso(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


# --- webhook auth ------------------------------------------------------------


def test_webhook_auth_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "")
    assert revenuecat_service.verify_webhook_auth("anything") is False


def test_webhook_auth_matches_configured_value(monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_auth_header", "s3cret")
    assert revenuecat_service.verify_webhook_auth("s3cret") is True
    assert revenuecat_service.verify_webhook_auth("nope") is False
    assert revenuecat_service.verify_webhook_auth(None) is False


# --- webhook parsing ---------------------------------------------------------


def test_parse_initial_purchase_active(monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "premium")
    parsed = revenuecat_service.parse_webhook_event({
        "event": {
            "type": "INITIAL_PURCHASE",
            "app_user_id": "user-1",
            "product_id": "kida.premium.yearly",
            "entitlement_ids": ["premium"],
            "expiration_at_ms": _future_ms(365),
            "store": "APP_STORE",
            "original_transaction_id": "orig-1",
        }
    })
    assert parsed.status == IapSubscriptionStatus.active
    assert parsed.product_id == "kida.premium.yearly"
    assert parsed.platform == IapPlatform.ios
    assert parsed.store_transaction_id == "orig-1"
    assert parsed.expires_at > datetime.now(timezone.utc)


def test_parse_expiration_sets_expired():
    parsed = revenuecat_service.parse_webhook_event({
        "event": {
            "type": "EXPIRATION",
            "app_user_id": "user-1",
            "product_id": "kida.premium.monthly",
            "expiration_at_ms": _past_ms(1),
            "store": "PLAY_STORE",
        }
    })
    assert parsed.status == IapSubscriptionStatus.expired
    assert parsed.platform == IapPlatform.android


def test_parse_billing_issue_sets_grace():
    parsed = revenuecat_service.parse_webhook_event({
        "event": {
            "type": "BILLING_ISSUE",
            "app_user_id": "user-1",
            "product_id": "kida.premium.monthly",
            "expiration_at_ms": _past_ms(1),
            "grace_period_expiration_at_ms": _future_ms(5),
            "store": "PLAY_STORE",
        }
    })
    assert parsed.status == IapSubscriptionStatus.grace
    assert parsed.expires_at > datetime.now(timezone.utc)


def test_parse_cancellation_keeps_access_until_expiry():
    # Auto-renew off but the paid period has not ended: still entitled.
    parsed = revenuecat_service.parse_webhook_event({
        "event": {
            "type": "CANCELLATION",
            "app_user_id": "user-1",
            "product_id": "kida.premium.monthly",
            "expiration_at_ms": _future_ms(10),
            "store": "APP_STORE",
        }
    })
    assert parsed.status == IapSubscriptionStatus.active


def test_parse_test_event_ignored():
    assert revenuecat_service.parse_webhook_event({"event": {"type": "TEST"}}) is None


def test_parse_other_entitlement_ignored(monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "premium")
    parsed = revenuecat_service.parse_webhook_event({
        "event": {
            "type": "INITIAL_PURCHASE",
            "app_user_id": "user-1",
            "entitlement_ids": ["some_other_thing"],
            "expiration_at_ms": _future_ms(30),
        }
    })
    assert parsed is None


def test_parse_missing_event_raises():
    with pytest.raises(AppError):
        revenuecat_service.parse_webhook_event({"nope": True})


# --- subscriber (REST) parsing ----------------------------------------------


def test_parse_subscriber_active(monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "premium")
    payload = {
        "subscriber": {
            "entitlements": {
                "premium": {
                    "expires_date": _future_iso(20),
                    "product_identifier": "kida.premium.monthly",
                }
            },
            "subscriptions": {
                "kida.premium.monthly": {
                    "expires_date": _future_iso(20),
                    "store": "play_store",
                    "store_transaction_id": "gpa-123",
                }
            },
        }
    }
    parsed = revenuecat_service.parse_subscriber("user-1", payload)
    assert parsed.status == IapSubscriptionStatus.active
    assert parsed.product_id == "kida.premium.monthly"
    assert parsed.platform == IapPlatform.android
    assert parsed.store_transaction_id == "gpa-123"


def test_parse_subscriber_billing_issue_is_grace(monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "premium")
    payload = {
        "subscriber": {
            "entitlements": {
                "premium": {
                    "expires_date": _future_iso(3),
                    "product_identifier": "kida.premium.monthly",
                }
            },
            "subscriptions": {
                "kida.premium.monthly": {
                    "expires_date": _future_iso(3),
                    "billing_issues_detected_at": _past_iso(1),
                    "store": "app_store",
                }
            },
        }
    }
    parsed = revenuecat_service.parse_subscriber("user-1", payload)
    assert parsed.status == IapSubscriptionStatus.grace


def test_parse_subscriber_no_entitlement_returns_none(monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "premium")
    assert revenuecat_service.parse_subscriber("user-1", {"subscriber": {}}) is None


def test_parse_subscriber_expired(monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "premium")
    payload = {
        "subscriber": {
            "entitlements": {
                "premium": {
                    "expires_date": _past_iso(2),
                    "product_identifier": "kida.premium.monthly",
                }
            },
            "subscriptions": {"kida.premium.monthly": {"expires_date": _past_iso(2)}},
        }
    }
    parsed = revenuecat_service.parse_subscriber("user-1", payload)
    assert parsed.status == IapSubscriptionStatus.expired


# --- REST client -------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_fetch_entitlement(monkeypatch):
    monkeypatch.setattr(get_settings(), "revenuecat_entitlement_id", "premium")
    client = RevenueCatClient(api_key="secret")
    monkeypatch.setattr(
        client,
        "get_subscriber",
        AsyncMock(return_value={
            "subscriber": {
                "entitlements": {
                    "premium": {
                        "expires_date": _future_iso(15),
                        "product_identifier": "kida.premium.monthly",
                    }
                },
                "subscriptions": {
                    "kida.premium.monthly": {
                        "expires_date": _future_iso(15),
                        "store": "app_store",
                    }
                },
            }
        }),
    )
    parsed = await client.fetch_entitlement("user-1")
    assert parsed.status == IapSubscriptionStatus.active


@pytest.mark.asyncio
async def test_client_requires_api_key(monkeypatch):
    """RevenueCatClient falls back to the configured key when passed a blank
    one, so the env has to be cleared for this to test what it claims. Without
    that the call reached the real API — passing or failing on whether the
    machine running the tests happened to have a key set, and needing the
    network either way."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("REVENUECAT_API_KEY", "")
    try:
        client = RevenueCatClient(api_key="")
        with pytest.raises(AppError) as exc:
            await client.get_subscriber("user-1")
        assert exc.value.status_code == 503
    finally:
        get_settings.cache_clear()
