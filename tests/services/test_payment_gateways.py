"""The factory, and the parts of each adapter that do not need a live API.

Signature verification and webhook parsing are pure functions over bytes, so
they are tested for real. Checkout and verification are tested through a
stubbed transport: what matters there is that each adapter sends the shape its
provider documents and normalizes the answer into the same dataclass.
"""
import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings
from app.exceptions import AppError
from app.models.purchase import PaymentProvider
from app.services import payments


@pytest.fixture
def gateways_configured(monkeypatch):
    """Credentials for all four, so is_configured stops being the answer."""
    get_settings.cache_clear()
    for key, value in {
        "FLUTTERWAVE_SECRET_KEY": "flw-secret",
        "FLUTTERWAVE_SECRET_HASH": "flw-hash",
        "PAYSTACK_SECRET_KEY": "pk-secret",
        "SQUAD_SECRET_KEY": "squad-secret",
        "STRIPE_SECRET_KEY": "sk-secret",
        "STRIPE_WEBHOOK_SECRET": "whsec-secret",
    }.items():
        monkeypatch.setenv(key, value)
    yield
    get_settings.cache_clear()


def _response(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = json.dumps(payload or {})
    resp.json.return_value = payload or {}
    return resp


def _transport(response):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.request = AsyncMock(return_value=response)
    return client


# --- the factory -------------------------------------------------------------

@pytest.mark.parametrize("provider", list(PaymentProvider))
def test_every_provider_has_a_gateway(provider):
    gateway = payments.get_gateway(provider)
    assert gateway.provider is provider
    assert gateway.signature_header
    assert gateway.label


def test_factory_accepts_the_wire_name():
    assert payments.get_gateway("stripe").provider is PaymentProvider.stripe


def test_factory_rejects_an_unknown_provider():
    with pytest.raises(AppError, match="Unknown payment provider") as exc:
        payments.get_gateway("bitcoin")
    assert exc.value.status_code == 400
    # the message should tell the caller what it could have asked for
    assert "paystack" in str(exc.value.message)


def test_configured_providers_reflects_the_environment(gateways_configured):
    assert set(payments.configured_providers()) == set(PaymentProvider)


def test_unconfigured_gateway_refuses_with_503(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    try:
        with pytest.raises(AppError, match="Stripe is not configured") as exc:
            payments.get_gateway("stripe").require_configured()
        assert exc.value.status_code == 503
    finally:
        get_settings.cache_clear()


# --- webhook signatures ------------------------------------------------------

@pytest.mark.parametrize("provider", list(PaymentProvider))
def test_unsigned_webhook_is_rejected(gateways_configured, provider):
    gateway = payments.get_gateway(provider)
    assert gateway.verify_webhook(b'{"event":"x"}', None) is False
    assert gateway.verify_webhook(b'{"event":"x"}', "") is False


@pytest.mark.parametrize("provider", list(PaymentProvider))
def test_unconfigured_gateway_rejects_every_webhook(monkeypatch, provider):
    """An empty secret must not become a secret that anything matches."""
    get_settings.cache_clear()
    for key in ("FLUTTERWAVE_SECRET_HASH", "FLW_HASH", "PAYSTACK_SECRET_KEY",
                "SQUAD_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
        monkeypatch.setenv(key, "")
    try:
        gateway = payments.get_gateway(provider)
        for signature in ("", None, "anything"):
            assert gateway.verify_webhook(b"{}", signature) is False
    finally:
        get_settings.cache_clear()


def test_flutterwave_signature(gateways_configured):
    gateway = payments.get_gateway("flutterwave")
    assert gateway.verify_webhook(b"{}", "flw-hash") is True
    assert gateway.verify_webhook(b"{}", "wrong") is False


def test_paystack_signature(gateways_configured):
    gateway = payments.get_gateway("paystack")
    body = b'{"event":"charge.success"}'
    signature = hmac.new(b"pk-secret", body, hashlib.sha512).hexdigest()
    assert gateway.verify_webhook(body, signature) is True
    assert gateway.verify_webhook(body + b" ", signature) is False


def test_squad_signature_is_case_insensitive(gateways_configured):
    """Squad sends the digest uppercase."""
    gateway = payments.get_gateway("squad")
    body = b'{"Event":"charge_successful"}'
    signature = hmac.new(b"squad-secret", body, hashlib.sha512).hexdigest()
    assert gateway.verify_webhook(body, signature.upper()) is True
    assert gateway.verify_webhook(body, signature) is True
    assert gateway.verify_webhook(body, "nope") is False


def test_stripe_signature(gateways_configured):
    gateway = payments.get_gateway("stripe")
    body = b'{"type":"checkout.session.completed"}'
    ts = int(time.time())
    good = hmac.new(b"whsec-secret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert gateway.verify_webhook(body, f"t={ts},v1={good}") is True
    # a second signature during a secret rotation still passes
    assert gateway.verify_webhook(body, f"t={ts},v1=old,v1={good}") is True
    assert gateway.verify_webhook(body, f"t={ts},v1=deadbeef") is False
    assert gateway.verify_webhook(body, "garbage") is False


def test_stripe_rejects_a_replayed_signature(gateways_configured):
    """The digest is still valid; the timestamp is not."""
    gateway = payments.get_gateway("stripe")
    body = b'{"type":"checkout.session.completed"}'
    old = int(time.time()) - 3600
    signature = hmac.new(b"whsec-secret", f"{old}.".encode() + body, hashlib.sha256).hexdigest()
    assert gateway.verify_webhook(body, f"t={old},v1={signature}") is False


# --- webhook parsing ---------------------------------------------------------

@pytest.mark.parametrize("provider,body,reference", [
    ("flutterwave",
     {"event": "charge.completed", "data": {"status": "successful", "tx_ref": "ref-1", "id": 99}},
     "ref-1"),
    ("paystack",
     {"event": "charge.success", "data": {"reference": "ref-2", "id": 7}},
     "ref-2"),
    ("squad",
     {"Event": "charge_successful", "Body": {"transaction_ref": "ref-3"}},
     "ref-3"),
    ("stripe",
     {"type": "checkout.session.completed",
      "data": {"object": {"id": "cs_1", "payment_status": "paid"}}},
     "cs_1"),
])
def test_successful_payment_parses_to_a_reference(gateways_configured, provider, body, reference):
    event = payments.get_gateway(provider).parse_webhook(json.dumps(body).encode())
    assert event.is_payment_success is True
    assert event.reference == reference


@pytest.mark.parametrize("provider", list(PaymentProvider))
def test_parsing_never_raises_on_junk(gateways_configured, provider):
    """A webhook body is untrusted input; parsing must not be how it kills us."""
    gateway = payments.get_gateway(provider)
    for body in (b"", b"not json", b"[]", b"null", b'{"data": "not-an-object"}', b"{}"):
        event = gateway.parse_webhook(body)
        assert event.is_payment_success is False


def test_unsuccessful_flutterwave_charge_is_not_a_success(gateways_configured):
    body = json.dumps(
        {"event": "charge.completed", "data": {"status": "failed", "tx_ref": "r"}}
    ).encode()
    assert payments.get_gateway("flutterwave").parse_webhook(body).is_payment_success is False


# --- checkout, over a stubbed transport --------------------------------------

@pytest.mark.asyncio
async def test_paystack_checkout_sends_kobo(gateways_configured):
    """Callers work in naira; Paystack is billed in kobo."""
    transport = _transport(_response(200, {
        "status": True, "data": {"authorization_url": "https://pay/x"},
    }))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        session = await payments.get_gateway("paystack").create_checkout(
            amount=Decimal("2500.50"), currency="NGN", reference="ref-1",
            email="a@b.com", description="Loop", metadata={"user_id": "u1"},
        )
    assert session.checkout_url == "https://pay/x"
    assert transport.request.call_args.kwargs["json"]["amount"] == 250050


@pytest.mark.asyncio
async def test_stripe_checkout_sends_cents_and_string_metadata(gateways_configured):
    transport = _transport(_response(200, {"url": "https://stripe/x", "id": "cs_123"}))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        session = await payments.get_gateway("stripe").create_checkout(
            amount=Decimal("10.00"), currency="usd", reference="ref-1",
            email="a@b.com", description="Loop", metadata={"user_id": "u1", "quantity": 10},
        )
    form = transport.request.call_args.kwargs["data"]
    assert form["line_items[0][price_data][unit_amount]"] == "1000"
    assert form["metadata[quantity]"] == "10", "Stripe rejects non-string metadata"
    # Stripe verifies by session id, so that is the reference we keep.
    assert session.reference == "cs_123"


@pytest.mark.asyncio
async def test_flutterwave_checkout_sends_major_units(gateways_configured):
    transport = _transport(_response(200, {
        "status": "success", "data": {"link": "https://flw/x"},
    }))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        await payments.get_gateway("flutterwave").create_checkout(
            amount=Decimal("2500.50"), currency="NGN", reference="ref-1",
            email="a@b.com", description="Loop", metadata={},
        )
    assert transport.request.call_args.kwargs["json"]["amount"] == "2500.50"


@pytest.mark.asyncio
async def test_squad_checkout_sends_kobo(gateways_configured):
    transport = _transport(_response(200, {
        "status": 200, "data": {"checkout_url": "https://squad/x", "transaction_ref": "ref-1"},
    }))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        session = await payments.get_gateway("squad").create_checkout(
            amount=Decimal("100"), currency="NGN", reference="ref-1",
            email="a@b.com", description="Loop", metadata={},
        )
    assert transport.request.call_args.kwargs["json"]["amount"] == 10000
    assert session.reference == "ref-1"


# --- verification normalizes to one shape ------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider,payload,expected_amount", [
    ("paystack",
     {"status": True, "data": {"status": "success", "reference": "r", "amount": 250050,
                               "currency": "NGN", "metadata": {"user_id": "u1"}}},
     Decimal("2500.50")),
    ("flutterwave",
     {"status": "success", "data": {"status": "successful", "tx_ref": "r", "amount": 2500.50,
                                    "currency": "NGN", "meta": {"user_id": "u1"}, "id": 5}},
     Decimal("2500.50")),
    ("stripe",
     {"payment_status": "paid", "id": "cs_1", "amount_total": 250050,
      "currency": "usd", "metadata": {"user_id": "u1"}},
     Decimal("2500.50")),
    ("squad",
     {"status": 200, "data": {"transaction_status": "success", "transaction_ref": "r",
                              "transaction_amount": 250050, "metadata": {"user_id": "u1"}}},
     Decimal("2500.50")),
])
async def test_verification_normalizes_amount_and_metadata(
    gateways_configured, provider, payload, expected_amount
):
    """Whatever units a gateway counts in, callers get major units and the
    metadata they attached at checkout."""
    transport = _transport(_response(200, payload))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        verified = await payments.get_gateway(provider).verify_transaction("r")
    assert verified.succeeded is True
    assert verified.amount == expected_amount
    assert verified.metadata["user_id"] == "u1"


@pytest.mark.asyncio
async def test_gateway_outage_is_502_not_500(gateways_configured):
    transport = _transport(_response(503, {}))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        with pytest.raises(AppError, match="could not process this right now") as exc:
            await payments.get_gateway("paystack").verify_transaction("r")
    assert exc.value.status_code == 502


# --- Flutterwave v4 ----------------------------------------------------------

@pytest.fixture
def flutterwave_v4(monkeypatch, gateways_configured):
    """v4 client credentials on top of the v3 key the hosted page needs."""
    from app.services.payments.flutterwave import clear_token_cache

    get_settings.cache_clear()
    monkeypatch.setenv("FLUTTERWAVE_CLIENT_ID", "client-id")
    monkeypatch.setenv("FLUTTERWAVE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("FLUTTERWAVE_V4_BASE_URL", "https://v4.flw.test/")
    clear_token_cache()
    yield
    clear_token_cache()
    get_settings.cache_clear()


def _v4_transport(*responses, token_payload=None):
    """A client whose POST answers the token request and whose request()
    answers the API calls, in order."""
    client = _transport(responses[0] if responses else _response())
    client.request = AsyncMock(side_effect=list(responses))
    client.post = AsyncMock(return_value=_response(
        200, token_payload or {"access_token": "tok-1", "expires_in": 600,
                               "token_type": "Bearer"},
    ))
    return client


def _v4_signature(body: bytes, secret: str = "flw-hash") -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


V4_SUCCESS = {
    "id": "wbk_W5p6ktwU0jQ8RO4By860",
    "timestamp": 1735116884019,
    "type": "charge.completed",
    "data": {"id": "chg_Hq4oBRTJ4r", "amount": 2500, "currency": "NGN",
             "meta": {}, "reference": "ref-v4", "status": "succeeded"},
}


def test_flutterwave_v4_hmac_signature(gateways_configured):
    gateway = payments.get_gateway("flutterwave")
    body = json.dumps(V4_SUCCESS).encode()
    assert gateway.verify_webhook(body, _v4_signature(body)) is True
    assert gateway.verify_webhook(body + b" ", _v4_signature(body)) is False
    assert gateway.verify_webhook(body, _v4_signature(body, "other-secret")) is False


def test_flutterwave_reads_either_signature_header(gateways_configured):
    gateway = payments.get_gateway("flutterwave")
    assert gateway.signature_from({"flutterwave-signature": "v4", "verif-hash": "v3"}) == "v4"
    assert gateway.signature_from({"verif-hash": "v3"}) == "v3"
    assert gateway.signature_from({}) is None


def test_flutterwave_v4_webhook_parses(gateways_configured):
    event = payments.get_gateway("flutterwave").parse_webhook(json.dumps(V4_SUCCESS).encode())
    assert event.is_payment_success is True
    assert event.reference == "ref-v4"
    assert event.provider_transaction_id == "chg_Hq4oBRTJ4r"
    assert event.raw_event == "charge.completed"


@pytest.mark.parametrize("status", ["pending", "failed", "voided"])
def test_flutterwave_v4_unsuccessful_charge_is_not_a_success(gateways_configured, status):
    body = {**V4_SUCCESS, "data": {**V4_SUCCESS["data"], "status": status}}
    event = payments.get_gateway("flutterwave").parse_webhook(json.dumps(body).encode())
    assert event.is_payment_success is False


@pytest.mark.asyncio
async def test_flutterwave_v4_verifies_by_reference(flutterwave_v4):
    transport = _v4_transport(_response(200, {
        "status": "success",
        "data": [
            {"id": "chg_old", "reference": "ref-1", "status": "failed",
             "amount": 2500.5, "currency": "NGN", "meta": {}},
            {"id": "chg_new", "reference": "ref-1", "status": "succeeded",
             "amount": 2500.5, "currency": "NGN", "meta": {"user_id": "u1"}},
        ],
    }))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        verified = await payments.get_gateway("flutterwave").verify_transaction("ref-1")

    assert verified.succeeded is True
    assert verified.amount == Decimal("2500.5")
    assert verified.metadata == {"user_id": "u1"}
    assert verified.provider_transaction_id == "chg_new"

    token_call = transport.post.call_args
    assert token_call.kwargs["data"] == {
        "client_id": "client-id", "client_secret": "client-secret",
        "grant_type": "client_credentials",
    }
    method, url = transport.request.call_args.args
    assert (method, url) == ("GET", "https://v4.flw.test/charges")
    assert transport.request.call_args.kwargs["params"] == {"reference": "ref-1"}
    headers = transport.request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok-1"
    assert 12 <= len(headers["X-Trace-Id"]) <= 255 and headers["X-Trace-Id"].isalnum()


@pytest.mark.asyncio
async def test_flutterwave_v4_never_takes_another_references_charge(flutterwave_v4, monkeypatch):
    """If the filter were ignored, the first charge listed must not count."""
    monkeypatch.setenv("FLUTTERWAVE_SECRET_KEY", "")
    get_settings.cache_clear()
    transport = _v4_transport(_response(200, {"status": "success", "data": [
        {"id": "chg_x", "reference": "someone-else", "status": "succeeded", "amount": 1},
    ]}))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        verified = await payments.get_gateway("flutterwave").verify_transaction("ref-1")
    assert verified.succeeded is False
    assert verified.reference == "ref-1"


@pytest.mark.asyncio
async def test_flutterwave_falls_back_to_v3_when_v4_has_no_charge(flutterwave_v4):
    transport = _v4_transport(
        _response(200, {"status": "success", "data": []}),
        _response(200, {"status": "success", "data": {
            "status": "successful", "tx_ref": "ref-1", "amount": 10, "currency": "USD",
            "meta": {"user_id": "u1"}, "id": 77}}),
    )
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        verified = await payments.get_gateway("flutterwave").verify_transaction("ref-1")
    assert verified.succeeded is True
    assert verified.currency == "USD"
    v3_call = transport.request.call_args_list[1]
    assert v3_call.args[1].endswith("/transactions/verify_by_reference")
    assert v3_call.kwargs["headers"] == {"Authorization": "Bearer flw-secret"}


@pytest.mark.asyncio
async def test_flutterwave_v4_token_is_cached(flutterwave_v4):
    listing = {"status": "success", "data": []}
    transport = _v4_transport(_response(200, listing), _response(200, listing))
    gateway_v4_only = {"FLUTTERWAVE_SECRET_KEY": ""}
    with patch.dict("os.environ", gateway_v4_only):
        get_settings.cache_clear()
        with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
            await payments.get_gateway("flutterwave").verify_transaction("a")
            await payments.get_gateway("flutterwave").verify_transaction("b")
    get_settings.cache_clear()
    assert transport.post.await_count == 1
    assert transport.request.await_count == 2


@pytest.mark.asyncio
async def test_flutterwave_v4_bad_credentials_are_502(flutterwave_v4):
    transport = _v4_transport(_response(200, {}))
    transport.post = AsyncMock(return_value=_response(
        401, {"error": "unauthorized_client", "error_description": "Invalid client secret"}))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        with pytest.raises(AppError) as exc:
            await payments.get_gateway("flutterwave").verify_transaction("ref-1")
    assert exc.value.status_code == 502
    transport.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_flutterwave_v4_verifies_by_charge_id(flutterwave_v4):
    transport = _v4_transport(_response(200, {"status": "success", "data": {
        "id": "chg_1", "reference": "ref-1", "status": "succeeded", "amount": 5,
        "currency": "USD", "meta": {"type": "premium"}}}))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        verified = await payments.get_gateway("flutterwave").verify_transaction_by_id("chg_1")
    assert verified.succeeded is True
    assert verified.reference == "ref-1"
    assert transport.request.call_args.args == ("GET", "https://v4.flw.test/charges/chg_1")


@pytest.mark.asyncio
async def test_flutterwave_checkout_still_uses_the_hosted_page(flutterwave_v4):
    """v4 has no hosted page, so checkout keeps using v3 Checkout."""
    transport = _v4_transport(_response(200, {
        "status": "success", "data": {"link": "https://checkout.flutterwave.com/x"}}))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        session = await payments.get_gateway("flutterwave").create_checkout(
            amount=Decimal("10"), currency="USD", reference="ref-1",
            email="a@b.com", description="Premium", metadata={},
        )
    assert session.checkout_url == "https://checkout.flutterwave.com/x"
    assert transport.request.call_args.args[1].endswith("/payments")
    transport.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_credentials_are_a_502_not_the_payers_problem(gateways_configured):
    """Flutterwave answers a bad v3 key with 401 "Invalid Authorization key".
    That is our misconfiguration, so the payer must not see it as a 400."""
    transport = _transport(_response(401, {
        "status": "error", "message": "Invalid Authorization key", "data": None}))
    with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
        with pytest.raises(AppError) as exc:
            await payments.get_gateway("flutterwave").create_checkout(
                amount=Decimal("10"), currency="NGN", reference="ref-1",
                email="a@b.com", description="Loop", metadata={},
            )
    assert exc.value.status_code == 502
    assert "Authorization" not in str(exc.value.message)


@pytest.mark.asyncio
async def test_flutterwave_key_is_sent_without_stray_quotes_or_spaces(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("FLUTTERWAVE_SECRET_KEY", ' "FLWSECK_TEST-abc-X" \n')
    try:
        transport = _transport(_response(200, {
            "status": "success", "data": {"link": "https://flw/x"}}))
        with patch("app.services.payments.base.httpx.AsyncClient", return_value=transport):
            await payments.get_gateway("flutterwave").create_checkout(
                amount=Decimal("10"), currency="NGN", reference="ref-1",
                email="a@b.com", description="Loop", metadata={},
            )
        headers = transport.request.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer FLWSECK_TEST-abc-X"
    finally:
        get_settings.cache_clear()
