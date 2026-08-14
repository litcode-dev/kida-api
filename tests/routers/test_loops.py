import pytest
import uuid
from decimal import Decimal
from app.models.loop import Loop, Genre, TempoFeel
from app.models.user import User, UserRole
from app.services.auth_service import hash_password, create_access_token


async def _create_user(db, role=UserRole.user):
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"), full_name="Test", role=role,
    )
    db.add(user)
    await db.commit()
    return user


async def _create_loop(db, user_id, is_free=True, title="Test Loop", status="ready"):
    loop = Loop(
        id=uuid.uuid4(), title=title, slug=f"test-loop-{uuid.uuid4().hex[:6]}",
        status=status,
        genre=Genre.afrobeat, bpm=100, key="C major", duration=30,
        tempo_feel=TempoFeel.mid, tags=["test"], price=Decimal("4.99"),
        is_free=is_free, is_paid=not is_free, created_by=user_id,
    )
    db.add(loop)
    await db.commit()
    return loop


def _headers(user):
    token = create_access_token(str(user.id), user.role.value)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_loops_requires_auth(client):
    resp = await client.get("/api/v1/loops")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_loops_for_authenticated_user(client, db_session):
    user = await _create_user(db_session)
    await _create_loop(db_session, user.id)
    resp = await client.get("/api/v1/loops", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] >= 1


@pytest.mark.asyncio
async def test_get_loop_not_found(client, db_session):
    user = await _create_user(db_session)
    resp = await client.get(f"/api/v1/loops/{uuid.uuid4()}", headers=_headers(user))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_free_loop_requires_auth(client, db_session):
    user = await _create_user(db_session)
    loop = await _create_loop(db_session, user.id, is_free=True)
    resp = await client.get(f"/api/v1/loops/{loop.id}/download")
    assert resp.status_code == 403  # no auth header


# ── Reprocessing window (loop audio replaced via PUT) ────────────────────────
#
# update_loop clears file_s3_key/preview_s3_key and sets status="processing"
# until the Celery job republishes the encrypted file. Serving during that
# window used to return 200 with a URL ending in "/None" and null aes_key/aes_iv.

def _patch_s3():
    from unittest.mock import AsyncMock, patch
    return patch(
        "app.services.s3_service.get_download_url",
        new=AsyncMock(return_value="https://signed.example/file"),
    )


async def _create_ready_loop(db, user_id, is_free=True):
    loop = await _create_loop(db, user_id, is_free=is_free)
    loop.file_s3_key = "loops/encrypted/test.enc"
    loop.preview_s3_key = "loops/preview/test.mp3"
    loop.aes_key, loop.aes_iv = "key", "iv"
    loop.status = "ready"
    await db.commit()
    return loop


@pytest.mark.asyncio
async def test_download_ready_loop_succeeds(client, db_session):
    user = await _create_user(db_session)
    loop = await _create_ready_loop(db_session, user.id)
    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["data"]["signed_url"] == "https://signed.example/file"


@pytest.mark.asyncio
async def test_download_while_reprocessing_returns_409(client, db_session):
    user = await _create_user(db_session)
    loop = await _create_ready_loop(db_session, user.id)

    # Exactly what update_loop leaves behind when new audio is uploaded.
    loop.file_s3_key = None
    loop.preview_s3_key = None
    loop.status = "processing"
    await db_session.commit()

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "error"
    assert body["data"] == {"status": "processing"}


@pytest.mark.asyncio
async def test_download_while_reprocessing_does_not_consume_a_free_tier_slot(client, db_session):
    """The cap is a lifetime grant — a download that cannot work must not spend one."""
    from sqlalchemy import select, func
    from app.models.download_grant import DownloadGrant

    user = await _create_user(db_session)
    loop = await _create_ready_loop(db_session, user.id)
    loop.file_s3_key = None
    loop.status = "processing"
    await db_session.commit()

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert resp.status_code == 409

    grants = await db_session.scalar(
        select(func.count()).select_from(DownloadGrant).where(DownloadGrant.user_id == user.id)
    )
    assert grants == 0
    await db_session.refresh(loop)
    assert loop.download_count == 0


@pytest.mark.asyncio
async def test_download_recovers_once_processing_finishes(client, db_session):
    user = await _create_user(db_session)
    loop = await _create_ready_loop(db_session, user.id)
    loop.file_s3_key = None
    loop.status = "processing"
    await db_session.commit()

    with _patch_s3():
        blocked = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert blocked.status_code == 409

    # Celery job completes.
    loop.file_s3_key = "loops/encrypted/test.enc"
    loop.status = "ready"
    await db_session.commit()

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_download_rejects_ready_status_with_missing_file(client, db_session):
    """status can lie — a failed job or bad data leaves status=ready with no key."""
    user = await _create_user(db_session)
    loop = await _create_ready_loop(db_session, user.id)
    loop.file_s3_key = None
    await db_session.commit()

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_download_after_failed_processing_returns_409(client, db_session):
    user = await _create_user(db_session)
    loop = await _create_ready_loop(db_session, user.id)
    loop.file_s3_key = None
    loop.status = "failed"
    await db_session.commit()

    with _patch_s3():
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))
    assert resp.status_code == 409
    assert resp.json()["data"] == {"status": "failed"}


@pytest.mark.asyncio
async def test_preview_while_reprocessing_returns_409(client, db_session):
    user = await _create_user(db_session)
    loop = await _create_ready_loop(db_session, user.id)
    loop.preview_s3_key = None
    loop.file_s3_key = None
    loop.status = "processing"
    await db_session.commit()

    resp = await client.get(f"/api/v1/loops/{loop.id}/preview", headers=_headers(user))
    assert resp.status_code == 409


# ── is_free / is_paid stay in sync on update ─────────────────────────────────
#
# create_loop sets is_paid = not is_free. update_loop used to leave is_paid
# untouched, so a loop switched to free still reported is_paid=true.

@pytest.mark.asyncio
async def test_update_loop_to_free_flips_is_paid(db_session):
    from app.schemas.loop import LoopUpdate
    from app.services import loop_service

    user = await _create_user(db_session)
    loop = await _create_loop(db_session, user.id, is_free=False)
    assert (loop.is_free, loop.is_paid) == (False, True)

    updated, _ = await loop_service.update_loop(
        db_session, loop.id, LoopUpdate(is_free=True)
    )
    assert (updated.is_free, updated.is_paid) == (True, False)


@pytest.mark.asyncio
async def test_update_loop_to_paid_flips_is_paid(db_session):
    from app.schemas.loop import LoopUpdate
    from app.services import loop_service

    user = await _create_user(db_session)
    loop = await _create_loop(db_session, user.id, is_free=True)
    assert (loop.is_free, loop.is_paid) == (True, False)

    updated, _ = await loop_service.update_loop(
        db_session, loop.id, LoopUpdate(is_free=False)
    )
    assert (updated.is_free, updated.is_paid) == (False, True)


@pytest.mark.asyncio
async def test_update_loop_without_is_free_leaves_flags_alone(db_session):
    from app.schemas.loop import LoopUpdate
    from app.services import loop_service

    user = await _create_user(db_session)
    loop = await _create_loop(db_session, user.id, is_free=False)

    updated, _ = await loop_service.update_loop(
        db_session, loop.id, LoopUpdate(title="Renamed")
    )
    assert updated.title == "Renamed"
    assert (updated.is_free, updated.is_paid) == (False, True)


@pytest.mark.asyncio
async def test_public_catalogue_omits_loops_that_are_not_ready(client, db_session):
    """A processing loop 409s on preview and download, so listing it puts
    something in the catalogue nobody can play."""
    user = await _create_user(db_session)
    await _create_loop(db_session, user.id, title="Playable")
    await _create_loop(db_session, user.id, title="Still Encoding", status="processing")

    resp = await client.get("/api/v1/loops", headers=_headers(user))

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [i["title"] for i in data["items"]] == ["Playable"]
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_producer_listing_still_shows_its_own_processing_loops(client, db_session):
    producer = await _create_user(db_session, role=UserRole.producer)
    await _create_loop(db_session, producer.id, title="Playable")
    await _create_loop(db_session, producer.id, title="Still Encoding", status="processing")

    resp = await client.get("/api/v1/producer/loops", headers=_headers(producer))

    assert resp.status_code == 200
    titles = sorted(i["title"] for i in resp.json()["data"]["items"])
    assert titles == ["Playable", "Still Encoding"]


@pytest.mark.asyncio
async def test_out_of_range_pagination_is_clamped_not_rejected(client, db_session):
    """All four catalogues clamp rather than 422, and report back what they
    actually used so a client can trust the echoed values."""
    user = await _create_user(db_session)
    await _create_loop(db_session, user.id)

    for path in ("/api/v1/loops", "/api/v1/drum-kits", "/api/v1/stem-packs", "/api/v1/drones"):
        resp = await client.get(f"{path}?page=0&page_size=100000", headers=_headers(user))
        assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text[:120]}"
        data = resp.json()["data"]
        assert data["page"] == 1, path
        assert data["page_size"] == 100, path
