"""Monthly download allowance: 20 loops, 5 drones, 5 drum kits per calendar month."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.services.payments import CheckoutSession

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.drum_kit import DrumKit, DrumSample
from app.models.iap_subscription import (
    IapSubscription, IapSubscriptionSource, IapSubscriptionStatus,
)
from app.models.loop import Genre, Loop, TempoFeel
from app.models.monthly_download_usage import MonthlyDownloadUsage, MonthlyQuotaType
from app.models.user import User, UserRole
from app.services import monthly_quota_service
from app.services.auth_service import create_access_token, hash_password


async def _make_user(db):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("pass1234"), full_name="Downloader",
        role=UserRole.user, is_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _subscribe(db, user_id):
    """Premium clears the free-tier caps, leaving the monthly allowance as the
    only limit — which is the case these numbers are really for."""
    db.add(IapSubscription(
        user_id=user_id, product_id="kida.premium.monthly",
        store_transaction_id=str(uuid.uuid4()),
        status=IapSubscriptionStatus.active,
        source=IapSubscriptionSource.store,
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    ))
    await db.commit()


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _make_loop(db, user_id):
    loop = Loop(
        id=uuid.uuid4(), title="L", slug=f"l-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat, bpm=100, duration=10, tempo_feel=TempoFeel.mid,
        tags=[], price=Decimal("0.00"), is_free=True, is_paid=False,
        created_by=user_id, file_s3_key="loops/x.enc", status="ready",
    )
    db.add(loop)
    await db.commit()
    return loop


async def _make_drone(db, user_id):
    drone = Drone(
        id=uuid.uuid4(), title=f"D-{uuid.uuid4().hex[:6]}", price=Decimal("0.00"),
        is_free=True, created_by=user_id,
    )
    db.add(drone)
    await db.flush()
    for key in (MusicalKey.C, MusicalKey.D):
        db.add(DronePad(
            id=uuid.uuid4(), drone_id=drone.id, key=key, duration=30,
            status="ready", file_s3_key=f"drones/{key.value}.enc",
        ))
    await db.commit()
    return drone


async def _make_kit(db, user_id):
    kit = DrumKit(
        id=uuid.uuid4(), title="K", slug=f"k-{uuid.uuid4().hex[:8]}",
        is_free=True, tags=[], created_by=user_id,
    )
    db.add(kit)
    await db.flush()
    db.add(DrumSample(
        id=uuid.uuid4(), drum_kit_id=kit.id, label="Kick", status="ready",
        file_s3_key="samples/k.enc", aes_key="k", aes_iv="iv", duration=1,
    ))
    await db.commit()
    return kit


def _patch_s3():
    # The store holds what the rows say it holds. A test about quota or
    # entitlement should not depend on reaching it — and must not send a real
    # request to find out.
    return patch.multiple(
        "app.services.s3_service",
        get_download_url=AsyncMock(return_value="https://signed.example/file"),
        object_exists=AsyncMock(return_value=True),
    )


async def _spend(db, user_id, item_type, n):
    """Fill the allowance without going through the endpoints."""
    period = monthly_quota_service.current_period()
    for _ in range(n):
        db.add(MonthlyDownloadUsage(
            user_id=user_id, period=period, item_type=item_type, item_id=str(uuid.uuid4()),
        ))
    await db.commit()


# ── Limits come from config ──────────────────────────────────────────────────

def test_default_allowances():
    settings = get_settings()
    assert settings.monthly_loop_downloads == 20
    assert settings.monthly_drone_downloads == 5
    assert settings.monthly_drum_kit_downloads == 5


def test_period_is_the_utc_calendar_month():
    assert monthly_quota_service.current_period(
        datetime(2026, 8, 9, 23, 59, tzinfo=timezone.utc)
    ) == "2026-08"
    assert monthly_quota_service.current_period(
        datetime(2026, 12, 31, tzinfo=timezone.utc)
    ) == "2026-12"


def test_reset_rolls_over_the_year():
    assert monthly_quota_service.next_period_start(
        datetime(2026, 12, 15, tzinfo=timezone.utc)
    ) == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert monthly_quota_service.next_period_start(
        datetime(2026, 8, 9, tzinfo=timezone.utc)
    ) == datetime(2026, 9, 1, tzinfo=timezone.utc)


# ── Loops ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_download_spends_one_slot(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 200
    assert await monthly_quota_service.used(db_session, user.id, MonthlyQuotaType.loop) == 1


@pytest.mark.asyncio
async def test_re_downloading_the_same_loop_is_free(client, db_session):
    """The app re-fetches when files go missing on the device."""
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        for _ in range(3):
            resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
            assert resp.status_code == 200

    assert await monthly_quota_service.used(db_session, user.id, MonthlyQuotaType.loop) == 1


@pytest.mark.asyncio
async def test_loop_download_is_refused_at_the_monthly_limit(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 20)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "monthly_download_limit"
    assert body["type"] == "loop"
    assert body["limit"] == 20
    assert body["resets_at"]


@pytest.mark.asyncio
async def test_the_twentieth_loop_still_succeeds(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 19)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 200
    assert await monthly_quota_service.used(db_session, user.id, MonthlyQuotaType.loop) == 20


@pytest.mark.asyncio
async def test_an_item_taken_last_month_does_not_count_now(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    # A full allowance, but recorded against a previous period.
    for _ in range(20):
        db_session.add(MonthlyDownloadUsage(
            user_id=user.id, period="2020-01",
            item_type=MonthlyQuotaType.loop, item_id=str(uuid.uuid4()),
        ))
    await db_session.commit()
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_one_users_downloads_do_not_touch_anothers(client, db_session):
    spent = await _make_user(db_session)
    fresh = await _make_user(db_session)
    await _subscribe(db_session, spent.id)
    await _subscribe(db_session, fresh.id)
    await _spend(db_session, spent.id, MonthlyQuotaType.loop, 20)
    loop = await _make_loop(db_session, fresh.id)

    with _patch_s3():
        assert (await client.get(
            f"/api/v1/loops/{loop.id}/download", headers=_headers(spent)
        )).status_code == 429
        assert (await client.get(
            f"/api/v1/loops/{loop.id}/download", headers=_headers(fresh)
        )).status_code == 200


@pytest.mark.asyncio
async def test_a_refused_download_records_nothing(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 20)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    # Still exactly the 20 that were there, and no download was recorded.
    assert await monthly_quota_service.used(db_session, user.id, MonthlyQuotaType.loop) == 20
    await db_session.refresh(loop)
    assert loop.download_count == 0


# ── Drum kits ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drum_kit_download_spends_one_slot(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    kit = await _make_kit(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/drum-kits/{kit.id}/download", headers=_headers(user))

    assert resp.status_code == 200
    assert await monthly_quota_service.used(db_session, user.id, MonthlyQuotaType.drum_kit) == 1


@pytest.mark.asyncio
async def test_drum_kit_download_is_refused_at_five(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.drum_kit, 5)
    kit = await _make_kit(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/drum-kits/{kit.id}/download", headers=_headers(user))

    assert resp.status_code == 429
    assert resp.json()["type"] == "drum_kit"
    assert resp.json()["limit"] == 5


# ── Drones ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drone_download_spends_one_slot(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    drone = await _make_drone(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/drones/{drone.id}/download", headers=_headers(user))

    assert resp.status_code == 200
    assert await monthly_quota_service.used(db_session, user.id, MonthlyQuotaType.drone) == 1


@pytest.mark.asyncio
async def test_several_pads_of_one_drone_cost_a_single_slot(client, db_session):
    """"5 drones" means five drone groups, not five pads."""
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    drone = await _make_drone(db_session, user.id)

    with _patch_s3():
        for key in ("C", "D"):
            resp = await client.get(
                f"/api/v1/drones/{drone.id}/download?key={key}", headers=_headers(user)
            )
            assert resp.status_code == 200

    assert await monthly_quota_service.used(db_session, user.id, MonthlyQuotaType.drone) == 1


@pytest.mark.asyncio
async def test_drone_download_is_refused_at_five(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.drone, 5)
    drone = await _make_drone(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/drones/{drone.id}/download", headers=_headers(user))

    assert resp.status_code == 429
    assert resp.json()["type"] == "drone"


# ── The three allowances are independent ─────────────────────────────────────

@pytest.mark.asyncio
async def test_spending_loops_does_not_block_drum_kits(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 20)
    kit = await _make_kit(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/drum-kits/{kit.id}/download", headers=_headers(user))

    assert resp.status_code == 200


# ── Quota endpoint ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quota_endpoint_reports_the_allowance(client, db_session):
    user = await _make_user(db_session)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 3)

    resp = await client.get("/api/v1/downloads/quota", headers=_headers(user))

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["period"] == monthly_quota_service.current_period()
    assert data["items"]["loop"] == {
        "used": 3, "limit": 20, "remaining": 17, "unlimited": False,
    }
    assert data["items"]["drone"] == {
        "used": 0, "limit": 5, "remaining": 5, "unlimited": False,
    }
    assert data["items"]["drum_kit"] == {
        "used": 0, "limit": 5, "remaining": 5, "unlimited": False,
    }
    assert data["resets_at"]


@pytest.mark.asyncio
async def test_quota_endpoint_requires_auth(client):
    assert (await client.get("/api/v1/downloads/quota")).status_code == 403


@pytest.mark.asyncio
async def test_remaining_never_goes_negative(client, db_session):
    """A limit lowered after the fact must not report a negative remainder."""
    user = await _make_user(db_session)
    await _spend(db_session, user.id, MonthlyQuotaType.drone, 9)

    resp = await client.get("/api/v1/downloads/quota", headers=_headers(user))

    assert resp.json()["data"]["items"]["drone"] == {
        "used": 9, "limit": 5, "remaining": 0, "unlimited": False,
    }


# ── Bookkeeping ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_usage_rows_go_when_the_account_does(client, db_session, fake_redis):
    """The table is CASCADE on users — a purge must not strand rows."""
    from app.services import auth_service

    user = await _make_user(db_session)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 2)

    with patch("app.services.email_service.send_email", new=AsyncMock()), \
         patch("app.services.s3_service.delete_object", new=AsyncMock()), \
         patch(
             "app.tasks.notification_tasks.send_account_deleted_admin_notification.delay",
             new=lambda *a, **kw: None,
         ):
        await auth_service.delete_user(db_session, user, fake_redis, None)

    assert await db_session.scalar(
        select(func.count()).select_from(MonthlyDownloadUsage)
        .where(MonthlyDownloadUsage.user_id == user.id)
    ) == 0


# ── "unlimited" turns a cap off ──────────────────────────────────────────────

@pytest.fixture
def unlimited_loops(monkeypatch):
    """Set MONTHLY_LOOP_DOWNLOADS=unlimited for one test."""
    get_settings.cache_clear()
    monkeypatch.setenv("MONTHLY_LOOP_DOWNLOADS", "unlimited")
    yield
    get_settings.cache_clear()


def test_unlimited_parses_to_no_cap(monkeypatch):
    from app.config import Settings

    for token in ("unlimited", "UNLIMITED", " Unlimited ", "none", "off"):
        monkeypatch.setenv("MONTHLY_LOOP_DOWNLOADS", token)
        assert Settings().monthly_loop_downloads is None


def test_zero_means_zero_not_unlimited(monkeypatch):
    """0 must block downloads, not uncap them."""
    from app.config import Settings

    monkeypatch.setenv("MONTHLY_LOOP_DOWNLOADS", "0")
    assert Settings().monthly_loop_downloads == 0


def test_blank_and_nonsense_values_are_rejected(monkeypatch):
    """A stray "MONTHLY_LOOP_DOWNLOADS=" must fail loudly, not uncap silently."""
    from pydantic import ValidationError
    from app.config import Settings

    for bad in ("", "   ", "-1", "lots"):
        monkeypatch.setenv("MONTHLY_LOOP_DOWNLOADS", bad)
        with pytest.raises(ValidationError):
            Settings()


@pytest.mark.asyncio
async def test_unlimited_lets_downloads_past_the_old_cap(
    client, db_session, unlimited_loops
):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 20)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unlimited_records_no_usage(client, db_session, unlimited_loops):
    """Nothing to count, so nothing is written."""
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        assert (await client.get(
            f"/api/v1/loops/{loop.id}/download", headers=_headers(user)
        )).status_code == 200

    assert await monthly_quota_service.used(db_session, user.id, MonthlyQuotaType.loop) == 0


@pytest.mark.asyncio
async def test_unlimited_is_reported_by_the_quota_endpoint(
    client, db_session, unlimited_loops
):
    user = await _make_user(db_session)

    resp = await client.get("/api/v1/downloads/quota", headers=_headers(user))

    loop_quota = resp.json()["data"]["items"]["loop"]
    assert loop_quota == {
        "used": 0, "limit": "unlimited", "remaining": "unlimited", "unlimited": True,
    }
    # The other two keep their caps.
    assert resp.json()["data"]["items"]["drone"]["unlimited"] is False


@pytest.mark.asyncio
async def test_unlimited_on_one_type_does_not_uncap_the_others(
    client, db_session, unlimited_loops
):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.drum_kit, 5)
    kit = await _make_kit(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/drum-kits/{kit.id}/download", headers=_headers(user))

    assert resp.status_code == 429


# ── Paying for downloads past the allowance ──────────────────────────────────
#
# Credits are pre-paid (a card cannot be charged inside a download request) and
# shared across all three types, so one purchase covers whichever runs out.

async def _give_credits(db, user, n):
    user.download_extra_credits = n
    await db.commit()
    await db.refresh(user)


@pytest.mark.asyncio
async def test_a_credit_is_spent_instead_of_refusing(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 20)
    await _give_credits(db_session, user, 3)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 200
    await db_session.refresh(user)
    assert user.download_extra_credits == 2


@pytest.mark.asyncio
async def test_credits_are_untouched_while_the_allowance_lasts(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _give_credits(db_session, user, 3)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 200
    await db_session.refresh(user)
    assert user.download_extra_credits == 3


@pytest.mark.asyncio
async def test_running_out_of_credits_refuses_again(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 20)
    await _give_credits(db_session, user, 1)

    first = await _make_loop(db_session, user.id)
    second = await _make_loop(db_session, user.id)

    with _patch_s3():
        assert (await client.get(
            f"/api/v1/loops/{first.id}/download", headers=_headers(user)
        )).status_code == 200
        resp = await client.get(
            f"/api/v1/loops/{second.id}/download", headers=_headers(user)
        )

    assert resp.status_code == 429
    assert resp.json()["extra_credits"] == 0
    await db_session.refresh(user)
    assert user.download_extra_credits == 0


@pytest.mark.asyncio
async def test_re_downloading_a_paid_item_costs_nothing(client, db_session):
    """A download that was paid for behaves like any other taken this month."""
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 20)
    await _give_credits(db_session, user, 2)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        for _ in range(3):
            assert (await client.get(
                f"/api/v1/loops/{loop.id}/download", headers=_headers(user)
            )).status_code == 200

    await db_session.refresh(user)
    assert user.download_extra_credits == 1


@pytest.mark.asyncio
async def test_credits_are_shared_across_types(client, db_session):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _spend(db_session, user.id, MonthlyQuotaType.loop, 20)
    await _spend(db_session, user.id, MonthlyQuotaType.drum_kit, 5)
    await _give_credits(db_session, user, 2)
    loop = await _make_loop(db_session, user.id)
    kit = await _make_kit(db_session, user.id)

    with _patch_s3():
        assert (await client.get(
            f"/api/v1/loops/{loop.id}/download", headers=_headers(user)
        )).status_code == 200
        assert (await client.get(
            f"/api/v1/drum-kits/{kit.id}/download", headers=_headers(user)
        )).status_code == 200

    await db_session.refresh(user)
    assert user.download_extra_credits == 0


@pytest.mark.asyncio
async def test_credits_do_not_apply_to_an_uncapped_type(
    client, db_session, unlimited_loops
):
    user = await _make_user(db_session)
    await _subscribe(db_session, user.id)
    await _give_credits(db_session, user, 2)
    loop = await _make_loop(db_session, user.id)

    with _patch_s3():
        assert (await client.get(
            f"/api/v1/loops/{loop.id}/download", headers=_headers(user)
        )).status_code == 200

    await db_session.refresh(user)
    assert user.download_extra_credits == 2


@pytest.mark.asyncio
async def test_quota_endpoint_reports_the_credit_balance(client, db_session):
    user = await _make_user(db_session)
    await _give_credits(db_session, user, 7)

    resp = await client.get("/api/v1/downloads/quota", headers=_headers(user))

    assert resp.json()["data"]["extra_credits"] == 7


# ── Buying credits ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_buying_extra_downloads_returns_a_checkout_url(client, db_session):
    user = await _make_user(db_session)

    with patch(
        "app.services.payments.paystack.PaystackGateway.create_checkout",
        new=AsyncMock(return_value=CheckoutSession(
            checkout_url="https://pay.example/x", reference="ref-1")),
    ) as init:
        resp = await client.post(
            "/api/v1/subscriptions/downloads/extras/initiate",
            json={"provider": "paystack"}, headers=_headers(user),
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["checkout_url"] == "https://pay.example/x"
    assert data["quantity"] == 10
    # The webhook keys off this metadata to know what was bought.
    assert init.call_args.kwargs["metadata"]["type"] == "download_extras"


@pytest.mark.asyncio
async def test_buying_extra_downloads_needs_no_subscription(client, db_session):
    """Anyone who used up their allowance can buy more."""
    user = await _make_user(db_session)

    with patch(
        "app.services.payments.paystack.PaystackGateway.create_checkout",
        new=AsyncMock(return_value=CheckoutSession(
            checkout_url="https://pay.example/x", reference="ref-1")),
    ):
        resp = await client.post(
            "/api/v1/subscriptions/downloads/extras/initiate",
            json={"provider": "paystack"}, headers=_headers(user),
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_buying_extra_downloads_requires_auth(client):
    resp = await client.post(
        "/api/v1/subscriptions/downloads/extras/initiate", json={"provider": "paystack"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_the_webhook_credits_the_account(db_session):
    from app.services import subscription_service

    user = await _make_user(db_session)
    await subscription_service._process_download_extras_webhook(
        db_session, str(user.id), 10
    )

    await db_session.refresh(user)
    assert user.download_extra_credits == 10


@pytest.mark.asyncio
async def test_the_webhook_adds_to_an_existing_balance(db_session):
    from app.services import subscription_service

    user = await _make_user(db_session)
    await _give_credits(db_session, user, 4)
    await subscription_service._process_download_extras_webhook(
        db_session, str(user.id), 10
    )

    await db_session.refresh(user)
    assert user.download_extra_credits == 14


@pytest.mark.asyncio
async def test_download_credits_do_not_touch_ai_credits(db_session):
    from app.services import subscription_service

    user = await _make_user(db_session)
    await subscription_service._process_download_extras_webhook(
        db_session, str(user.id), 10
    )

    await db_session.refresh(user)
    assert user.ai_extra_credits == 0
