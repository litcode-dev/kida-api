"""The generic webhook route, driven once per gateway.

One route now serves every provider, so these tests are parameterized across
all four: whatever is added to the registry next gets the same coverage by
appearing in PaymentProvider.
"""
import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.loop import Genre, Loop, TempoFeel
from app.models.purchase import PaymentProvider, Purchase
from app.models.user import User, UserRole
from app.services import payments
from app.services.auth_service import hash_password
from app.services.payments import VerifiedTransaction


@pytest.fixture
def gateways_configured(monkeypatch):
    get_settings.cache_clear()
    for key, value in {
        "FLUTTERWAVE_SECRET_HASH": "flw-hash",
        "PAYSTACK_SECRET_KEY": "pk-secret",
        "SQUAD_SECRET_KEY": "squad-secret",
        "STRIPE_WEBHOOK_SECRET": "whsec-secret",
        "FLUTTERWAVE_SECRET_KEY": "flw-secret",
        "STRIPE_SECRET_KEY": "sk-secret",
    }.items():
        monkeypatch.setenv(key, value)
    yield
    get_settings.cache_clear()


async def _make_user(db):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("pass1234"), full_name="Buyer",
        role=UserRole.user, is_verified=True,
    )
    db.add(user)
    await db.commit()
    return user


async def _make_loop(db, user_id):
    loop = Loop(
        id=uuid.uuid4(), title="L", slug=f"l-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat, bpm=100, duration=10, tempo_feel=TempoFeel.mid,
        tags=[], price=Decimal("10.00"), is_free=False, is_paid=True,
        created_by=user_id, file_s3_key="loops/x.enc", status="ready",
    )
    db.add(loop)
    await db.commit()
    return loop


def _body_and_signature(provider: str, reference: str):
    """A webhook body this provider would send, signed the way it signs."""
    bodies = {
        "flutterwave": {"event": "charge.completed",
                        "data": {"status": "successful", "tx_ref": reference, "id": 1}},
        "paystack": {"event": "charge.success", "data": {"reference": reference}},
        "squad": {"Event": "charge_successful", "Body": {"transaction_ref": reference}},
        "stripe": {"type": "checkout.session.completed",
                   "data": {"object": {"id": reference, "payment_status": "paid"}}},
    }
    body = json.dumps(bodies[provider]).encode()
    if provider == "flutterwave":
        signature = "flw-hash"
    elif provider == "paystack":
        signature = hmac.new(b"pk-secret", body, hashlib.sha512).hexdigest()
    elif provider == "squad":
        signature = hmac.new(b"squad-secret", body, hashlib.sha512).hexdigest().upper()
    else:
        ts = int(time.time())
        digest = hmac.new(b"whsec-secret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        signature = f"t={ts},v1={digest}"
    return body, signature


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [p.value for p in PaymentProvider])
async def test_signed_webhook_records_the_purchase(
    client, db_session, gateways_configured, provider
):
    user = await _make_user(db_session)
    loop = await _make_loop(db_session, user.id)
    reference = f"ref-{uuid.uuid4().hex[:8]}"
    body, signature = _body_and_signature(provider, reference)

    verified = VerifiedTransaction(
        reference=reference, succeeded=True, amount=Decimal("10.00"), currency="NGN",
        metadata={"user_id": str(user.id), "loop_id": str(loop.id)},
    )
    gateway_cls = type(payments.get_gateway(provider))
    with patch.object(gateway_cls, "verify_transaction",
                      new=AsyncMock(return_value=verified)):
        resp = await client.post(
            f"/api/v1/payments/webhook/{provider}", content=body,
            headers={payments.get_gateway(provider).signature_header: signature},
        )

    assert resp.status_code == 200
    purchase = await db_session.scalar(
        select(Purchase).where(Purchase.payment_reference == reference)
    )
    assert purchase is not None
    assert purchase.payment_provider == PaymentProvider(provider)
    assert purchase.amount_paid == Decimal("10.00")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [p.value for p in PaymentProvider])
async def test_unsigned_webhook_is_rejected(client, gateways_configured, provider):
    body, _ = _body_and_signature(provider, "ref-x")
    resp = await client.post(f"/api/v1/payments/webhook/{provider}", content=body)
    assert resp.status_code == 400
    assert resp.json()["message"] == "Invalid webhook signature"


@pytest.mark.asyncio
async def test_webhook_for_an_unknown_provider_is_400(client, gateways_configured):
    resp = await client.post("/api/v1/payments/webhook/bitcoin", content=b"{}")
    assert resp.status_code == 400
    assert "Unknown payment provider" in resp.json()["message"]


@pytest.mark.asyncio
async def test_webhook_body_cannot_choose_the_buyer(
    client, db_session, gateways_configured
):
    """The security property behind re-verifying: metadata comes from the
    gateway's answer, so a body naming a different user changes nothing."""
    user = await _make_user(db_session)
    attacker = await _make_user(db_session)
    loop = await _make_loop(db_session, user.id)
    reference = f"ref-{uuid.uuid4().hex[:8]}"

    body = json.dumps({
        "event": "charge.success",
        "data": {"reference": reference,
                 "metadata": {"user_id": str(attacker.id), "loop_id": str(loop.id)},
                 "amount": 999999},
    }).encode()
    signature = hmac.new(b"pk-secret", body, hashlib.sha512).hexdigest()

    verified = VerifiedTransaction(
        reference=reference, succeeded=True, amount=Decimal("10.00"), currency="NGN",
        metadata={"user_id": str(user.id), "loop_id": str(loop.id)},
    )
    with patch(
        "app.services.payments.paystack.PaystackGateway.verify_transaction",
        new=AsyncMock(return_value=verified),
    ):
        resp = await client.post(
            "/api/v1/payments/webhook/paystack", content=body,
            headers={"x-paystack-signature": signature},
        )

    assert resp.status_code == 200
    purchase = await db_session.scalar(
        select(Purchase).where(Purchase.payment_reference == reference)
    )
    assert purchase.user_id == user.id, "the body must not be able to name the buyer"
    assert purchase.amount_paid == Decimal("10.00"), "nor the amount"


@pytest.mark.asyncio
async def test_unverified_transaction_grants_nothing(
    client, db_session, gateways_configured
):
    reference = f"ref-{uuid.uuid4().hex[:8]}"
    body, signature = _body_and_signature("paystack", reference)
    verified = VerifiedTransaction(
        reference=reference, succeeded=False, amount=Decimal("0"), currency="NGN",
    )
    with patch(
        "app.services.payments.paystack.PaystackGateway.verify_transaction",
        new=AsyncMock(return_value=verified),
    ):
        resp = await client.post(
            "/api/v1/payments/webhook/paystack", content=body,
            headers={"x-paystack-signature": signature},
        )
    assert resp.status_code == 200
    assert await db_session.scalar(
        select(Purchase).where(Purchase.payment_reference == reference)
    ) is None


@pytest.mark.asyncio
async def test_webhook_without_metadata_is_not_an_error(
    client, db_session, gateways_configured
):
    """A charge that did not start from one of our checkouts: nothing to grant,
    and no reason to 500 and be retried forever."""
    reference = f"ref-{uuid.uuid4().hex[:8]}"
    body, signature = _body_and_signature("paystack", reference)
    verified = VerifiedTransaction(
        reference=reference, succeeded=True, amount=Decimal("10.00"),
        currency="NGN", metadata={},
    )
    with patch(
        "app.services.payments.paystack.PaystackGateway.verify_transaction",
        new=AsyncMock(return_value=verified),
    ):
        resp = await client.post(
            "/api/v1/payments/webhook/paystack", content=body,
            headers={"x-paystack-signature": signature},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_providers_endpoint_lists_what_is_configured(client, gateways_configured):
    resp = await client.get("/api/v1/payments/providers")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data["supported"]) == {p.value for p in PaymentProvider}
    assert set(data["providers"]) == {p.value for p in PaymentProvider}
