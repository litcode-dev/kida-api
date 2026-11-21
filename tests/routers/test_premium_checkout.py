"""Kiɗa Premium bought by card from the desktop app.

Gateways are faked at the adapter boundary (create_checkout /
verify_transaction), the same way the purchase webhook tests do it, so the
router, service, ledger and entitlement are all exercised for real.
"""
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models.iap_subscription import (
    IapSubscription,
    IapSubscriptionSource,
    IapSubscriptionStatus,
)
from app.models.premium_payment import PremiumPayment
from app.models.user import User, UserRole
from app.services import payments
from app.services.auth_service import create_access_token, hash_password
from app.services.payments import CheckoutSession, VerifiedTransaction

PAYSTACK = type(payments.get_gateway("paystack"))


@pytest.fixture
def paystack_configured(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "pk-secret")
    yield
    get_settings.cache_clear()


async def _user(db):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("pass1234"), full_name="Desk Top",
        role=UserRole.user, is_verified=True,
    )
    db.add(user)
    await db.commit()
    return user


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _sub(db, user_id, *, source, expires_in, locked=False):
    sub = IapSubscription(
        user_id=user_id, product_id="kida.premium.monthly",
        store_transaction_id=str(uuid.uuid4()), status=IapSubscriptionStatus.active,
        source=source, admin_locked=locked,
        expires_at=datetime.now(timezone.utc) + expires_in,
    )
    db.add(sub)
    await db.commit()
    return sub


def _verified(user, reference, *, plan="monthly", succeeded=True,
              amount=None, kobo=None, currency="NGN"):
    kobo = kobo if kobo is not None else (200000 if plan == "monthly" else 2000000)
    return VerifiedTransaction(
        reference=reference, succeeded=succeeded,
        amount=amount if amount is not None else Decimal(kobo) / 100,
        currency=currency,
        metadata={"user_id": str(user.id), "type": "premium", "plan": plan,
                  "amount_kobo": kobo},
    )


async def _confirm(client, user, reference, verified):
    with patch.object(PAYSTACK, "verify_transaction", new=AsyncMock(return_value=verified)):
        return await client.post(
            "/api/v1/subscriptions/premium/confirm",
            json={"provider": "paystack", "payment_reference": reference},
            headers=_auth(user),
        )


async def _entitlement(client, user):
    return (await client.get("/api/v1/subscriptions/me", headers=_auth(user))).json()


def _days_left(expires_at_iso):
    left = datetime.fromisoformat(expires_at_iso) - datetime.now(timezone.utc)
    return round(left.total_seconds() / 86400)


# -- plans -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plans_lists_monthly_and_yearly_prices(client):
    resp = await client.get("/api/v1/subscriptions/premium/plans")
    assert resp.status_code == 200
    plans = {p["plan"]: p for p in resp.json()["data"]["plans"]}
    assert plans["monthly"]["amount"] == "2000"
    assert plans["monthly"]["period_days"] == 30
    assert plans["yearly"]["amount"] == "20000"
    assert plans["yearly"]["period_days"] == 365
    assert plans["yearly"]["product_id"] == "kida.premium.yearly"


# -- checkout ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_sends_plan_price_and_user_to_the_gateway(client, db_session):
    user = await _user(db_session)
    fake = AsyncMock(return_value=CheckoutSession("https://pay.example/abc", "ref-1"))
    with patch.object(PAYSTACK, "create_checkout", new=fake):
        resp = await client.post(
            "/api/v1/subscriptions/premium/checkout",
            json={"plan": "yearly", "provider": "paystack"}, headers=_auth(user),
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["checkout_url"] == "https://pay.example/abc"
    assert data["payment_reference"] == "ref-1"
    assert data["amount"] == "20000"
    kwargs = fake.call_args.kwargs
    assert kwargs["amount"] == Decimal("20000")
    assert kwargs["currency"] == "NGN"
    assert kwargs["email"] == user.email
    assert kwargs["metadata"]["type"] == "premium"
    assert kwargs["metadata"]["plan"] == "yearly"
    assert kwargs["metadata"]["user_id"] == str(user.id)
    assert kwargs["metadata"]["amount_kobo"] == 2000000


@pytest.mark.asyncio
async def test_checkout_requires_auth(client):
    resp = await client.post(
        "/api/v1/subscriptions/premium/checkout",
        json={"plan": "monthly", "provider": "paystack"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [IapSubscriptionSource.store, IapSubscriptionSource.revenuecat])
async def test_checkout_refused_with_active_store_subscription(client, db_session, source):
    user = await _user(db_session)
    await _sub(db_session, user.id, source=source, expires_in=timedelta(days=10))
    resp = await client.post(
        "/api/v1/subscriptions/premium/checkout",
        json={"plan": "monthly", "provider": "paystack"}, headers=_auth(user),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_checkout_allowed_after_store_subscription_lapsed(client, db_session):
    user = await _user(db_session)
    await _sub(db_session, user.id, source=IapSubscriptionSource.store,
               expires_in=timedelta(days=-1))
    fake = AsyncMock(return_value=CheckoutSession("https://pay.example/x", "ref-x"))
    with patch.object(PAYSTACK, "create_checkout", new=fake):
        resp = await client.post(
            "/api/v1/subscriptions/premium/checkout",
            json={"plan": "monthly", "provider": "paystack"}, headers=_auth(user),
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_checkout_refused_when_admin_locked(client, db_session):
    user = await _user(db_session)
    await _sub(db_session, user.id, source=IapSubscriptionSource.admin,
               expires_in=timedelta(days=-1), locked=True)
    resp = await client.post(
        "/api/v1/subscriptions/premium/checkout",
        json={"plan": "monthly", "provider": "paystack"}, headers=_auth(user),
    )
    assert resp.status_code == 403


# -- confirm -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_payment_grants_nothing(client, db_session):
    user = await _user(db_session)
    resp = await _confirm(client, user, "ref-p", _verified(user, "ref-p", succeeded=False))
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "paid": False,
        "entitlement": {"active": False, "expires_at": None, "product_id": None},
    }


@pytest.mark.asyncio
async def test_confirmed_payment_unlocks_premium(client, db_session):
    user = await _user(db_session)
    resp = await _confirm(client, user, "ref-ok", _verified(user, "ref-ok"))

    data = resp.json()["data"]
    assert data["paid"] is True
    assert data["entitlement"]["active"] is True
    assert data["entitlement"]["product_id"] == "kida.premium.monthly"
    assert _days_left(data["entitlement"]["expires_at"]) == 30
    # The same entitlement every premium check reads.
    assert (await _entitlement(client, user))["active"] is True

    sub = await db_session.scalar(select(IapSubscription).where(IapSubscription.user_id == user.id))
    assert sub.source == IapSubscriptionSource.web
    payment = await db_session.scalar(select(PremiumPayment))
    assert payment.reference == "ref-ok"
    assert payment.amount == Decimal("2000.00")


@pytest.mark.asyncio
async def test_yearly_payment_grants_a_year(client, db_session):
    user = await _user(db_session)
    resp = await _confirm(client, user, "ref-y", _verified(user, "ref-y", plan="yearly"))
    ent = resp.json()["data"]["entitlement"]
    assert ent["product_id"] == "kida.premium.yearly"
    assert _days_left(ent["expires_at"]) == 365


@pytest.mark.asyncio
async def test_confirming_twice_counts_the_payment_once(client, db_session):
    user = await _user(db_session)
    first = await _confirm(client, user, "ref-2x", _verified(user, "ref-2x"))
    second = await _confirm(client, user, "ref-2x", _verified(user, "ref-2x"))

    assert first.json()["data"]["entitlement"] == second.json()["data"]["entitlement"]
    assert await db_session.scalar(select(func.count()).select_from(PremiumPayment)) == 1


@pytest.mark.asyncio
async def test_paying_early_adds_to_the_time_left(client, db_session):
    user = await _user(db_session)
    await _sub(db_session, user.id, source=IapSubscriptionSource.web,
               expires_in=timedelta(days=10))
    resp = await _confirm(client, user, "ref-early", _verified(user, "ref-early"))
    assert _days_left(resp.json()["data"]["entitlement"]["expires_at"]) == 40


@pytest.mark.asyncio
async def test_lapsed_premium_restarts_from_today(client, db_session):
    user = await _user(db_session)
    await _sub(db_session, user.id, source=IapSubscriptionSource.web,
               expires_in=timedelta(days=-20))
    resp = await _confirm(client, user, "ref-late", _verified(user, "ref-late"))
    assert _days_left(resp.json()["data"]["entitlement"]["expires_at"]) == 30


@pytest.mark.asyncio
async def test_cannot_confirm_someone_elses_payment(client, db_session):
    owner = await _user(db_session)
    other = await _user(db_session)
    resp = await _confirm(client, other, "ref-o", _verified(owner, "ref-o"))
    assert resp.status_code == 404
    assert (await _entitlement(client, other))["active"] is False
    assert (await _entitlement(client, owner))["active"] is False


@pytest.mark.asyncio
async def test_non_premium_payment_is_not_found(client, db_session):
    user = await _user(db_session)
    verified = VerifiedTransaction(
        reference="ref-dl", succeeded=True, amount=Decimal("500"), currency="NGN",
        metadata={"user_id": str(user.id), "type": "download_extras", "quantity": 10},
    )
    resp = await _confirm(client, user, "ref-dl", verified)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_short_payment_is_not_applied(client, db_session):
    user = await _user(db_session)
    resp = await _confirm(
        client, user, "ref-short", _verified(user, "ref-short", amount=Decimal("100"))
    )
    assert resp.json()["data"]["paid"] is False
    assert (await _entitlement(client, user))["active"] is False


@pytest.mark.asyncio
async def test_wrong_currency_is_not_applied(client, db_session):
    user = await _user(db_session)
    resp = await _confirm(
        client, user, "ref-usd",
        _verified(user, "ref-usd", amount=Decimal("2000"), currency="USD"),
    )
    assert resp.json()["data"]["paid"] is False


@pytest.mark.asyncio
async def test_price_change_after_checkout_does_not_reject_the_payment(
    client, db_session, monkeypatch
):
    # Checkout was opened at ₦2,000; the price has since gone up.
    get_settings.cache_clear()
    monkeypatch.setenv("SUBSCRIPTION_MONTHLY_PRICE", "300000")
    try:
        user = await _user(db_session)
        resp = await _confirm(client, user, "ref-old", _verified(user, "ref-old", kobo=200000))
        assert resp.json()["data"]["paid"] is True
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_payment_not_applied_over_admin_lock(client, db_session):
    user = await _user(db_session)
    await _sub(db_session, user.id, source=IapSubscriptionSource.admin,
               expires_in=timedelta(days=-1), locked=True)
    resp = await _confirm(client, user, "ref-lock", _verified(user, "ref-lock"))
    assert resp.json()["data"]["entitlement"]["active"] is False
    assert await db_session.scalar(select(func.count()).select_from(PremiumPayment)) == 0


# -- webhook -----------------------------------------------------------------


def _paystack_webhook(reference):
    body = json.dumps({"event": "charge.success", "data": {"reference": reference}}).encode()
    signature = hmac.new(b"pk-secret", body, hashlib.sha512).hexdigest()
    return body, {"x-paystack-signature": signature}


@pytest.mark.asyncio
async def test_webhook_unlocks_premium_and_confirm_does_not_double_it(
    client, db_session, paystack_configured
):
    user = await _user(db_session)
    verified = _verified(user, "ref-wh")
    body, headers = _paystack_webhook("ref-wh")
    headers = {payments.get_gateway("paystack").signature_header: headers["x-paystack-signature"]}

    with patch.object(PAYSTACK, "verify_transaction", new=AsyncMock(return_value=verified)):
        resp = await client.post(
            "/api/v1/subscriptions/webhook/paystack", content=body, headers=headers
        )
    assert resp.status_code == 200
    ent = await _entitlement(client, user)
    assert ent["active"] is True
    assert _days_left(ent["expires_at"]) == 30

    # The app confirms after the webhook already did the work.
    again = await _confirm(client, user, "ref-wh", verified)
    assert _days_left(again.json()["data"]["entitlement"]["expires_at"]) == 30
    assert await db_session.scalar(select(func.count()).select_from(PremiumPayment)) == 1
