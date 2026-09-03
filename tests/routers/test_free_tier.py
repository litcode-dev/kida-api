"""Server-side free-tier download cap enforcement (download_grants)."""
import pytest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, func

from app.config import get_settings
from app.models.download_grant import DownloadGrant, DownloadGrantType
from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.drum_kit import DrumKit, DrumSample
from app.models.iap_subscription import IapSubscription, IapSubscriptionStatus
from app.models.loop import Loop, Genre, TempoFeel
from app.models.purchase import Purchase
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password

FREE_TIER_LIMIT_BODY = {"error": "free_tier_limit", "type": "loop", "limit": 2}


async def _create_user(db):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"), full_name="Test", role=UserRole.user,
    )
    db.add(user)
    await db.commit()
    return user


def _headers(user):
    token = create_access_token(str(user.id), user.role.value)
    return {"Authorization": f"Bearer {token}"}


async def _create_loop(db, user_id, is_free=True):
    loop = Loop(
        id=uuid.uuid4(), title="Test Loop", slug=f"test-loop-{uuid.uuid4().hex[:6]}",
        genre=Genre.afrobeat, bpm=100, key="C major", duration=30,
        tempo_feel=TempoFeel.mid, tags=["test"], price=Decimal("4.99"),
        is_free=is_free, is_paid=not is_free, created_by=user_id,
        file_s3_key="loops/test.enc",
    )
    db.add(loop)
    await db.commit()
    return loop


async def _create_drum_kit(db, user_id, is_free=True):
    kit = DrumKit(
        id=uuid.uuid4(), title="Test Kit", slug=f"test-kit-{uuid.uuid4().hex[:6]}",
        is_free=is_free, created_by=user_id,
    )
    db.add(kit)
    await db.flush()
    sample = DrumSample(
        id=uuid.uuid4(), drum_kit_id=kit.id, label="Kick",
        file_s3_key="drums/kick.enc", aes_key="k", aes_iv="iv",
        duration=1, status="ready",
    )
    db.add(sample)
    await db.commit()
    return kit


async def _create_drone(db, user_id, title="Warm Pad", keys=(MusicalKey.C, MusicalKey.D), is_free=True):
    drone = Drone(
        id=uuid.uuid4(), title=title, price=None if is_free else Decimal("4.99"),
        is_free=is_free, created_by=user_id,
    )
    db.add(drone)
    await db.flush()
    for key in keys:
        db.add(DronePad(
            id=uuid.uuid4(), drone_id=drone.id, key=key, duration=30,
            file_s3_key=f"drones/{key.value}.enc", status="ready",
        ))
    await db.commit()
    return drone


async def _subscribe(db, user_id, status=IapSubscriptionStatus.active):
    sub = IapSubscription(
        user_id=user_id, platform="android", product_id="kida.premium.monthly",
        store_transaction_id=str(uuid.uuid4()), status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db.add(sub)
    await db.commit()
    return sub


async def _grant_count(db, user_id, item_type=None):
    q = select(func.count()).select_from(DownloadGrant).where(DownloadGrant.user_id == user_id)
    if item_type is not None:
        q = q.where(DownloadGrant.item_type == item_type)
    return await db.scalar(q)


def _patch_s3():
    # The store holds what the rows say it holds. A test about quota or
    # entitlement should not depend on reaching it — and must not send a real
    # request to find out.
    return patch.multiple(
        "app.services.s3_service",
        get_download_url=AsyncMock(return_value="https://signed.example/file"),
        object_exists=AsyncMock(return_value=True),
    )


# ── Loops ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_under_cap_grants_and_serves(client, db_session):
    user = await _create_user(db_session)
    loop = await _create_loop(db_session, user.id)
    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["data"]["signed_url"]
    assert await _grant_count(db_session, user.id, DownloadGrantType.loop) == 1


@pytest.mark.asyncio
async def test_loop_re_download_is_idempotent(client, db_session):
    user = await _create_user(db_session)
    loop = await _create_loop(db_session, user.id)
    with _patch_s3():
        first = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
        second = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert first.status_code == 200
    assert second.status_code == 200
    assert await _grant_count(db_session, user.id, DownloadGrantType.loop) == 1


@pytest.mark.asyncio
async def test_loop_over_cap_returns_403_with_exact_body(client, db_session):
    user = await _create_user(db_session)
    loops = [await _create_loop(db_session, user.id) for _ in range(3)]
    with _patch_s3():
        for loop in loops[:2]:
            resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
            assert resp.status_code == 200
        resp = await client.get(f"/api/v1/loops/{loops[2].id}/download", headers=_headers(user))
    assert resp.status_code == 403
    assert resp.json() == FREE_TIER_LIMIT_BODY
    assert await _grant_count(db_session, user.id, DownloadGrantType.loop) == 2


@pytest.mark.asyncio
async def test_loop_over_cap_granted_item_still_downloadable(client, db_session):
    user = await _create_user(db_session)
    loops = [await _create_loop(db_session, user.id) for _ in range(2)]
    with _patch_s3():
        for loop in loops:
            await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
        # At the cap now, but re-downloading a granted item must still work.
        resp = await client.get(f"/api/v1/loops/{loops[0].id}/download", headers=_headers(user))
    assert resp.status_code == 200
    assert await _grant_count(db_session, user.id, DownloadGrantType.loop) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [IapSubscriptionStatus.active, IapSubscriptionStatus.grace])
async def test_subscriber_bypass_creates_no_grants(client, db_session, status):
    user = await _create_user(db_session)
    await _subscribe(db_session, user.id, status=status)
    loops = [await _create_loop(db_session, user.id) for _ in range(3)]
    with _patch_s3():
        for loop in loops:
            resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
            assert resp.status_code == 200
    assert await _grant_count(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_expired_subscription_does_not_bypass(client, db_session):
    user = await _create_user(db_session)
    sub = await _subscribe(db_session, user.id, status=IapSubscriptionStatus.expired)
    sub.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()
    loop = await _create_loop(db_session, user.id)
    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert resp.status_code == 200
    assert await _grant_count(db_session, user.id, DownloadGrantType.loop) == 1


@pytest.mark.asyncio
async def test_purchased_paid_loop_creates_no_grant(client, db_session):
    user = await _create_user(db_session)
    loop = await _create_loop(db_session, user.id, is_free=False)
    db_session.add(Purchase(user_id=user.id, loop_id=loop.id, item_id=loop.id))
    await db_session.commit()
    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert resp.status_code == 200
    assert await _grant_count(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_owned_free_loop_creates_no_grant(client, db_session):
    user = await _create_user(db_session)
    loop = await _create_loop(db_session, user.id)
    db_session.add(Purchase(user_id=user.id, item_id=loop.id))
    await db_session.commit()
    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert resp.status_code == 200
    assert await _grant_count(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_loop_cap_config_override_takes_effect(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "free_tier_loop_downloads", 3)
    user = await _create_user(db_session)
    loops = [await _create_loop(db_session, user.id) for _ in range(4)]
    with _patch_s3():
        for loop in loops[:3]:
            resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
            assert resp.status_code == 200
        resp = await client.get(f"/api/v1/loops/{loops[3].id}/download", headers=_headers(user))
    assert resp.status_code == 403
    assert resp.json() == {"error": "free_tier_limit", "type": "loop", "limit": 3}


# ── Drum kits ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drum_kit_under_cap_grants_and_serves(client, db_session):
    user = await _create_user(db_session)
    kit = await _create_drum_kit(db_session, user.id)
    with _patch_s3():
        resp = await client.get(f"/api/v1/drum-kits/{kit.id}/download", headers=_headers(user))
    assert resp.status_code == 200
    assert await _grant_count(db_session, user.id, DownloadGrantType.drum_kit) == 1


@pytest.mark.asyncio
async def test_drum_kit_over_cap_returns_403_with_exact_body(client, db_session):
    user = await _create_user(db_session)
    kits = [await _create_drum_kit(db_session, user.id) for _ in range(2)]
    with _patch_s3():
        first = await client.get(f"/api/v1/drum-kits/{kits[0].id}/download", headers=_headers(user))
        assert first.status_code == 200
        # Re-download of the granted kit still works at the cap.
        again = await client.get(f"/api/v1/drum-kits/{kits[0].id}/download", headers=_headers(user))
        assert again.status_code == 200
        resp = await client.get(f"/api/v1/drum-kits/{kits[1].id}/download", headers=_headers(user))
    assert resp.status_code == 403
    assert resp.json() == {"error": "free_tier_limit", "type": "drum_kit", "limit": 1}
    assert await _grant_count(db_session, user.id, DownloadGrantType.drum_kit) == 1


@pytest.mark.asyncio
async def test_drum_kit_subscriber_bypass(client, db_session):
    user = await _create_user(db_session)
    await _subscribe(db_session, user.id)
    kits = [await _create_drum_kit(db_session, user.id) for _ in range(2)]
    with _patch_s3():
        for kit in kits:
            resp = await client.get(f"/api/v1/drum-kits/{kit.id}/download", headers=_headers(user))
            assert resp.status_code == 200
    assert await _grant_count(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_purchased_paid_drum_kit_creates_no_grant(client, db_session):
    user = await _create_user(db_session)
    kit = await _create_drum_kit(db_session, user.id, is_free=False)
    db_session.add(Purchase(user_id=user.id, drum_kit_id=kit.id))
    await db_session.commit()
    with _patch_s3():
        resp = await client.get(f"/api/v1/drum-kits/{kit.id}/download", headers=_headers(user))
    assert resp.status_code == 200
    assert await _grant_count(db_session, user.id) == 0


# ── Drone groups and pads ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_free_drone_group_download_allows_one_then_blocks(client, db_session):
    user = await _create_user(db_session)
    first = await _create_drone(db_session, user.id)
    second = await _create_drone(db_session, user.id)
    with _patch_s3():
        resp1 = await client.get(f"/api/v1/drones/{first.id}/download", headers=_headers(user))
        # Re-requesting the same group is idempotent — no new grant consumed.
        again = await client.get(f"/api/v1/drones/{first.id}/download", headers=_headers(user))
        resp2 = await client.get(f"/api/v1/drones/{second.id}/download", headers=_headers(user))
    assert resp1.status_code == 200
    assert resp1.json()["data"]["total"] == 2
    assert again.status_code == 200
    assert resp2.status_code == 403
    assert resp2.json() == {"error": "free_tier_limit", "type": "drone_group", "limit": 1}
    assert await _grant_count(db_session, user.id, DownloadGrantType.drone_group) == 1


@pytest.mark.asyncio
async def test_free_drone_group_download_with_subscription(client, db_session):
    user = await _create_user(db_session)
    await _subscribe(db_session, user.id)
    drone = await _create_drone(db_session, user.id)
    with _patch_s3():
        resp = await client.get(f"/api/v1/drones/{drone.id}/download", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 2
    assert await _grant_count(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_owned_paid_drone_group_always_downloadable(client, db_session):
    user = await _create_user(db_session)
    drone = await _create_drone(db_session, user.id, is_free=False)
    db_session.add(Purchase(user_id=user.id, item_id=drone.id))
    await db_session.commit()
    with _patch_s3():
        resp = await client.get(f"/api/v1/drones/{drone.id}/download", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 2
    assert await _grant_count(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_single_pad_download_consumes_one_drone_pad_grant(client, db_session):
    user = await _create_user(db_session)
    drone = await _create_drone(db_session, user.id)
    with _patch_s3():
        resp = await client.get(
            f"/api/v1/drones/{drone.id}/download", params={"key": "C"}, headers=_headers(user)
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["key"] == "C"
    grants = list(await db_session.scalars(
        select(DownloadGrant).where(DownloadGrant.user_id == user.id)
    ))
    assert len(grants) == 1
    assert grants[0].item_type == DownloadGrantType.drone_pad
    assert grants[0].item_id == f"{drone.id}:C"


@pytest.mark.asyncio
async def test_second_free_pad_key_is_refused_at_cap(client, db_session):
    user = await _create_user(db_session)
    drone = await _create_drone(db_session, user.id)
    with _patch_s3():
        first = await client.get(
            f"/api/v1/drones/{drone.id}/download", params={"key": "C"}, headers=_headers(user)
        )
        assert first.status_code == 200
        # Same key again is idempotent.
        again = await client.get(
            f"/api/v1/drones/{drone.id}/download", params={"key": "C"}, headers=_headers(user)
        )
        assert again.status_code == 200
        resp = await client.get(
            f"/api/v1/drones/{drone.id}/download", params={"key": "D"}, headers=_headers(user)
        )
    assert resp.status_code == 403
    assert resp.json() == {"error": "free_tier_limit", "type": "drone_pad", "limit": 1}
    assert await _grant_count(db_session, user.id, DownloadGrantType.drone_pad) == 1


@pytest.mark.asyncio
async def test_two_keys_are_two_grants_when_cap_allows(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "free_tier_drone_pad_downloads", 2)
    user = await _create_user(db_session)
    drone = await _create_drone(db_session, user.id)
    with _patch_s3():
        for key in ("C", "D"):
            resp = await client.get(
                f"/api/v1/drones/{drone.id}/download", params={"key": key}, headers=_headers(user)
            )
            assert resp.status_code == 200
    grants = list(await db_session.scalars(
        select(DownloadGrant.item_id).where(
            DownloadGrant.user_id == user.id,
            DownloadGrant.item_type == DownloadGrantType.drone_pad,
        )
    ))
    assert sorted(grants) == sorted([f"{drone.id}:C", f"{drone.id}:D"])


@pytest.mark.asyncio
async def test_pad_key_with_sharp_is_url_decodable(client, db_session):
    user = await _create_user(db_session)
    drone = await _create_drone(db_session, user.id, keys=(MusicalKey.C_sharp,))
    with _patch_s3():
        resp = await client.get(
            f"/api/v1/drones/{drone.id}/download", params={"key": "C#"}, headers=_headers(user)
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["items"][0]["key"] == "C#"


@pytest.mark.asyncio
async def test_unknown_pad_key_is_404(client, db_session):
    user = await _create_user(db_session)
    drone = await _create_drone(db_session, user.id, keys=(MusicalKey.C,))
    with _patch_s3():
        resp = await client.get(
            f"/api/v1/drones/{drone.id}/download", params={"key": "G"}, headers=_headers(user)
        )
    assert resp.status_code == 404
    assert await _grant_count(db_session, user.id) == 0


# ── Drone title downloads ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_title_download_full_group_allowed_once_then_blocked(client, db_session):
    user = await _create_user(db_session)
    await _create_drone(db_session, user.id, title="Warm Pad")
    await _create_drone(db_session, user.id, title="Cold Pad")
    with _patch_s3():
        first = await client.get("/api/v1/drones/titles/Warm Pad/download", headers=_headers(user))
        second = await client.get("/api/v1/drones/titles/Cold Pad/download", headers=_headers(user))
    assert first.status_code == 200
    assert second.status_code == 403
    assert second.json() == {"error": "free_tier_limit", "type": "drone_group", "limit": 1}


@pytest.mark.asyncio
async def test_title_download_with_subscription(client, db_session):
    user = await _create_user(db_session)
    await _subscribe(db_session, user.id)
    await _create_drone(db_session, user.id, title="Warm Pad")
    with _patch_s3():
        resp = await client.get("/api/v1/drones/titles/Warm Pad/download", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 2
    assert await _grant_count(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_title_download_unknown_title_is_404(client, db_session):
    user = await _create_user(db_session)
    resp = await client.get("/api/v1/drones/titles/Nope/download", headers=_headers(user))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_title_single_pad_shares_grant_with_group_endpoint(client, db_session):
    user = await _create_user(db_session)
    drone = await _create_drone(db_session, user.id, title="Warm Pad")
    with _patch_s3():
        # Grant via the group endpoint…
        first = await client.get(
            f"/api/v1/drones/{drone.id}/download", params={"key": "C"}, headers=_headers(user)
        )
        assert first.status_code == 200
        # …then the same musical pad via the title endpoint reuses the grant.
        via_title = await client.get(
            "/api/v1/drones/titles/Warm Pad/download", params={"key": "C"}, headers=_headers(user)
        )
        assert via_title.status_code == 200
        assert via_title.json()["data"]["total"] == 1
        # A different key of the same title is a second grant — refused at cap 1.
        resp = await client.get(
            "/api/v1/drones/titles/Warm Pad/download", params={"key": "D"}, headers=_headers(user)
        )
    assert resp.status_code == 403
    assert resp.json() == {"error": "free_tier_limit", "type": "drone_pad", "limit": 1}
    assert await _grant_count(db_session, user.id, DownloadGrantType.drone_pad) == 1
