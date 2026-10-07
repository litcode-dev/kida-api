"""Requesting an admin-published app: free apps email a link straight away,
paid apps return a checkout URL and email the 3-day link once the payment
webhook is verified."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.app_download_request import AppDownloadRequest
from app.models.desktop_app import DesktopApp
from app.services import s3_service
from app.services.payments import CheckoutSession, VerifiedTransaction


@pytest.fixture
def paystack_configured(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "pk-secret")
    yield
    get_settings.cache_clear()


@pytest.fixture
def email_task(monkeypatch):
    import app.routers.app_download as router_mod
    import app.tasks.notification_tasks as tasks_mod

    task = MagicMock()
    monkeypatch.setattr(router_mod, "send_app_download_email", task)
    monkeypatch.setattr(tasks_mod, "send_app_download_email", task)
    return task


async def _make_app(db, **overrides):
    fields = dict(
        name="Toniq",
        platforms={
            "macos": "r2://installers/toniq.dmg",
            "windows": "s3://win-bucket/toniq.exe",
            "linux": "https://cdn.example.com/toniq.AppImage",
        },
        is_paid=True, price=Decimal("15000.00"), currency="NGN",
    )
    fields.update(overrides)
    app = DesktopApp(**fields)
    db.add(app)
    await db.commit()
    return app


def _paystack_webhook(reference: str):
    body = json.dumps({"event": "charge.success", "data": {"reference": reference}}).encode()
    signature = hmac.new(b"pk-secret", body, hashlib.sha512).hexdigest()
    return body, {"x-paystack-signature": signature}


async def _start_paid_checkout(client):
    with patch(
        "app.services.payments.paystack.PaystackGateway.create_checkout",
        new=AsyncMock(return_value=CheckoutSession(
            checkout_url="https://checkout.paystack.com/abc", reference="ref-123",
        )),
    ) as create_checkout:
        resp = await client.post(
            "/api/v1/app/download-request",
            json={"email": "buyer@test.com", "os": "macos", "app_name": "toniq"},
        )
    return resp, create_checkout


@pytest.mark.asyncio
async def test_paid_app_returns_checkout_url_and_sends_nothing(
    client, db_session, paystack_configured, email_task
):
    await _make_app(db_session)
    resp, create_checkout = await _start_paid_checkout(client)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["payment_required"] is True
    assert data["checkout_url"] == "https://checkout.paystack.com/abc"
    assert data["amount"] == "15000.00"
    assert data["currency"] == "NGN"
    assert data["app_name"] == "Toniq"
    email_task.delay.assert_not_called()

    kwargs = create_checkout.call_args.kwargs
    assert kwargs["amount"] == Decimal("15000.00")
    assert kwargs["currency"] == "NGN"

    req = await db_session.scalar(select(AppDownloadRequest))
    assert req.status == "pending_payment"
    assert req.expires_at is None
    assert req.payment_reference == "ref-123"
    assert kwargs["metadata"]["app_download_request_id"] == str(req.id)

    # Not redeemable before payment.
    redeem = await client.get(f"/api/v1/app/download/{req.token}", follow_redirects=False)
    assert redeem.status_code == 402


@pytest.mark.asyncio
async def test_verified_payment_emails_a_3_day_link(
    client, db_session, paystack_configured, email_task, monkeypatch
):
    await _make_app(db_session)
    await _start_paid_checkout(client)
    req = await db_session.scalar(select(AppDownloadRequest))

    verified = VerifiedTransaction(
        reference="ref-123", succeeded=True, amount=Decimal("15000.00"), currency="NGN",
        metadata={"app_download_request_id": str(req.id)},
    )
    body, headers = _paystack_webhook("ref-123")
    with patch(
        "app.services.payments.paystack.PaystackGateway.verify_transaction",
        new=AsyncMock(return_value=verified),
    ):
        resp = await client.post("/api/v1/payments/webhook/paystack", content=body, headers=headers)
        # A redelivered webhook does not email again.
        await client.post("/api/v1/payments/webhook/paystack", content=body, headers=headers)

    assert resp.status_code == 200
    email_task.delay.assert_called_once_with(str(req.id))

    await db_session.refresh(req)
    assert req.status == "fulfilled"
    assert req.paid_at is not None
    delta = req.expires_at - datetime.now(timezone.utc)
    assert timedelta(days=2, hours=23) < delta <= timedelta(days=3)

    presign = AsyncMock(return_value="https://r2/signed-studio")
    monkeypatch.setattr(s3_service, "generate_r2_presigned_url", presign)
    redeem = await client.get(f"/api/v1/app/download/{req.token}", follow_redirects=False)
    assert redeem.status_code == 302
    assert redeem.headers["location"] == "https://r2/signed-studio"
    assert presign.call_args.args[0] == "toniq.dmg"
    assert presign.call_args.kwargs["bucket"] == "installers"


@pytest.mark.asyncio
async def test_underpayment_does_not_fulfil(
    client, db_session, paystack_configured, email_task
):
    await _make_app(db_session)
    await _start_paid_checkout(client)
    req = await db_session.scalar(select(AppDownloadRequest))

    verified = VerifiedTransaction(
        reference="ref-123", succeeded=True, amount=Decimal("100.00"), currency="NGN",
        metadata={"app_download_request_id": str(req.id)},
    )
    body, headers = _paystack_webhook("ref-123")
    with patch(
        "app.services.payments.paystack.PaystackGateway.verify_transaction",
        new=AsyncMock(return_value=verified),
    ):
        resp = await client.post("/api/v1/payments/webhook/paystack", content=body, headers=headers)

    assert resp.status_code == 200
    email_task.delay.assert_not_called()
    await db_session.refresh(req)
    assert req.status == "pending_payment"


@pytest.mark.asyncio
async def test_free_app_emails_link_and_redirects_to_https_url(
    client, db_session, email_task
):
    await _make_app(db_session, is_paid=False, price=None, currency=None)
    resp = await client.post(
        "/api/v1/app/download-request",
        json={"email": "free@test.com", "os": "linux", "app_name": "Toniq"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["payment_required"] is False
    email_task.delay.assert_called_once()

    req = await db_session.scalar(select(AppDownloadRequest))
    redeem = await client.get(f"/api/v1/app/download/{req.token}", follow_redirects=False)
    assert redeem.status_code == 302
    assert redeem.headers["location"] == "https://cdn.example.com/toniq.AppImage"


@pytest.mark.asyncio
async def test_unknown_or_inactive_app_is_404(client, db_session, email_task):
    await _make_app(db_session, is_active=False)
    for name in ("Toniq", "Nope"):
        resp = await client.post(
            "/api/v1/app/download-request",
            json={"email": "x@test.com", "os": "macos", "app_name": name},
        )
        assert resp.status_code == 404
    email_task.delay.assert_not_called()


@pytest.mark.asyncio
async def test_link_for_deleted_app_is_gone(client, db_session, email_task):
    app = await _make_app(db_session, is_paid=False, price=None, currency=None)
    await client.post(
        "/api/v1/app/download-request",
        json={"email": "gone@test.com", "os": "macos", "app_name": "Toniq"},
    )
    token = (await db_session.scalar(select(AppDownloadRequest))).token
    await db_session.delete(app)
    await db_session.commit()
    db_session.expire_all()

    redeem = await client.get(f"/api/v1/app/download/{token}", follow_redirects=False)
    assert redeem.status_code == 410


@pytest.mark.asyncio
async def test_public_app_list_hides_file_url(client, db_session):
    await _make_app(db_session)
    await _make_app(db_session, name="Hidden", is_active=False)
    resp = await client.get("/api/v1/app/apps")
    assert resp.status_code == 200
    items = resp.json()["data"]
    assert [a["name"] for a in items] == ["Toniq"]
    assert items[0]["available_os"] == ["linux", "macos", "windows"]
    assert "platforms" not in items[0]


@pytest.mark.asyncio
async def test_windows_build_is_presigned_from_s3(client, db_session, email_task, monkeypatch):
    await _make_app(db_session, is_paid=False, price=None, currency=None)
    await client.post(
        "/api/v1/app/download-request",
        json={"email": "win@test.com", "os": "windows", "app_name": "Toniq"},
    )
    req = await db_session.scalar(select(AppDownloadRequest))
    presign = AsyncMock(return_value="https://s3/signed-win")
    monkeypatch.setattr(s3_service, "generate_presigned_url", presign)
    redeem = await client.get(f"/api/v1/app/download/{req.token}", follow_redirects=False)
    assert redeem.headers["location"] == "https://s3/signed-win"
    assert presign.call_args.args[0] == "toniq.exe"
    assert presign.call_args.kwargs["bucket"] == "win-bucket"


@pytest.mark.asyncio
async def test_app_without_build_for_os_is_404(client, db_session, email_task):
    await _make_app(db_session, platforms={"macos": "r2://installers/toniq.dmg"})
    resp = await client.post(
        "/api/v1/app/download-request",
        json={"email": "l@test.com", "os": "linux", "app_name": "Toniq"},
    )
    assert resp.status_code == 404
    assert "Linux" in resp.json()["message"]
