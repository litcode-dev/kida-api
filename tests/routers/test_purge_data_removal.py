"""What a purge must actually erase.

The account row going away is not the same as the user's personal data going
away — these cover the parts that live outside the users table.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.models.app_download_request import AppDownloadRequest
from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.drum_kit import DrumKit, DrumSample
from app.models.loop import Genre, Loop, TempoFeel
from app.models.newsletter import NewsletterSubscriber
from app.models.stem_pack import Stem, StemPack
from app.models.user import User, UserRole
from app.services import auth_service
from app.services.auth_service import hash_password

EMAIL = "producer@test.com"


async def _make_user(db, email=EMAIL, role=UserRole.producer):
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=await hash_password("pass1234"),
        full_name="Producer",
        role=role,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _purge(db, user, redis):
    """Run the real hard delete, capturing the S3 keys it would remove."""
    deleted_keys = []

    async def _capture(key):
        deleted_keys.append(key)

    with patch("app.services.s3_service.delete_object", new=_capture), \
         patch("app.services.email_service.send_email", new=AsyncMock()), \
         patch(
             "app.tasks.notification_tasks.send_account_deleted_admin_notification.delay",
             new=lambda *a, **kw: None,
         ):
        await auth_service.delete_user(db, user, redis, None)
    return deleted_keys


async def _count(db, model, **where):
    q = select(func.count()).select_from(model)
    for col, val in where.items():
        q = q.where(getattr(model, col) == val)
    return await db.scalar(q)


# ── Personal data outside the users table ────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_removes_the_newsletter_subscription(db_session, fake_redis):
    user = await _make_user(db_session)
    db_session.add(NewsletterSubscriber(email=EMAIL))
    await db_session.commit()

    await _purge(db_session, user, fake_redis)

    assert await _count(db_session, NewsletterSubscriber, email=EMAIL) == 0


@pytest.mark.asyncio
async def test_purge_removes_an_unsubscribed_newsletter_row_too(db_session, fake_redis):
    """Unsubscribed rows keep the address on file — they have to go as well."""
    user = await _make_user(db_session)
    db_session.add(NewsletterSubscriber(
        email=EMAIL, is_active=False, unsubscribed_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    await _purge(db_session, user, fake_redis)

    assert await _count(db_session, NewsletterSubscriber, email=EMAIL) == 0


@pytest.mark.asyncio
async def test_purge_matches_the_newsletter_address_case_insensitively(db_session, fake_redis):
    user = await _make_user(db_session, email="Mixed.Case@Test.com")
    db_session.add(NewsletterSubscriber(email="mixed.case@test.com"))
    await db_session.commit()

    await _purge(db_session, user, fake_redis)

    assert await db_session.scalar(select(func.count()).select_from(NewsletterSubscriber)) == 0


@pytest.mark.asyncio
async def test_purge_leaves_other_peoples_subscriptions_alone(db_session, fake_redis):
    user = await _make_user(db_session)
    db_session.add(NewsletterSubscriber(email=EMAIL))
    db_session.add(NewsletterSubscriber(email="someone-else@test.com"))
    await db_session.commit()

    await _purge(db_session, user, fake_redis)

    remaining = set(await db_session.scalars(select(NewsletterSubscriber.email)))
    assert remaining == {"someone-else@test.com"}


@pytest.mark.asyncio
async def test_purge_removes_app_download_requests(db_session, fake_redis):
    user = await _make_user(db_session)
    db_session.add(AppDownloadRequest(
        email=EMAIL, os="macos", token=uuid.uuid4().hex,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ))
    db_session.add(AppDownloadRequest(
        email="other@test.com", os="windows", token=uuid.uuid4().hex,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ))
    await db_session.commit()

    await _purge(db_session, user, fake_redis)

    assert await _count(db_session, AppDownloadRequest, email=EMAIL) == 0
    assert await _count(db_session, AppDownloadRequest, email="other@test.com") == 1


# ── Stored files ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_deletes_a_loops_files(db_session, fake_redis):
    user = await _make_user(db_session)
    loop_id = uuid.uuid4()
    db_session.add(Loop(
        id=loop_id, title="L", slug=f"l-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat, bpm=100, duration=10, tempo_feel=TempoFeel.mid,
        tags=[], price=Decimal("1.00"), is_free=True, is_paid=False,
        created_by=user.id,
        file_s3_key="loops/encrypted/l.enc",
        preview_s3_key="loops/preview/l.mp3",
        thumbnail_s3_key="loops/thumb/l.jpg",
    ))
    await db_session.commit()

    keys = await _purge(db_session, user, fake_redis)

    assert "loops/encrypted/l.enc" in keys
    assert "loops/preview/l.mp3" in keys
    assert "loops/thumb/l.jpg" in keys
    # A loop stuck mid-processing still has its raw upload.
    assert f"loops/raw/{loop_id}.wav" in keys


@pytest.mark.asyncio
async def test_purge_deletes_stem_files(db_session, fake_redis):
    user = await _make_user(db_session)
    pack = StemPack(
        id=uuid.uuid4(), title="P", slug=f"p-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat, bpm=100, key="C major",
        price=Decimal("1.00"), created_by=user.id,
    )
    db_session.add(pack)
    await db_session.flush()
    db_session.add(Stem(
        id=uuid.uuid4(), stem_pack_id=pack.id, label="Kick", duration=5,
        file_s3_key="stems/s.enc", preview_s3_key="stems/s.mp3",
    ))
    await db_session.commit()

    keys = await _purge(db_session, user, fake_redis)

    assert {"stems/s.enc", "stems/s.mp3"} <= set(keys)


@pytest.mark.asyncio
async def test_purge_deletes_drum_kit_files(db_session, fake_redis):
    user = await _make_user(db_session)
    kit = DrumKit(
        id=uuid.uuid4(), title="K", slug=f"k-{uuid.uuid4().hex[:8]}",
        is_free=True, tags=[], created_by=user.id,
        thumbnail_s3_key="kits/k.jpg",
    )
    db_session.add(kit)
    await db_session.flush()
    sample_id = uuid.uuid4()
    db_session.add(DrumSample(
        id=sample_id, drum_kit_id=kit.id, label="Snare", status="ready",
        file_s3_key="samples/s.enc", preview_s3_key="samples/s.mp3",
    ))
    await db_session.commit()

    keys = await _purge(db_session, user, fake_redis)

    assert {"kits/k.jpg", "samples/s.enc", "samples/s.mp3"} <= set(keys)
    assert f"drum-kits/raw/{sample_id}.wav" in keys
    assert await _count(db_session, DrumSample, id=sample_id) == 0


@pytest.mark.asyncio
async def test_purge_deletes_drone_pad_files(db_session, fake_redis):
    user = await _make_user(db_session)
    drone_id = uuid.uuid4()
    drone = Drone(
        id=drone_id, title="D", price=Decimal("0.00"), is_free=True, created_by=user.id,
    )
    db_session.add(drone)
    await db_session.flush()
    db_session.add(DronePad(
        id=uuid.uuid4(), drone_id=drone_id, key=MusicalKey.C, duration=30, status="ready",
        file_s3_key="drones/d.enc", preview_s3_key="drones/d.mp3",
        thumbnail_s3_key="drones/d.jpg",
    ))
    await db_session.commit()

    keys = await _purge(db_session, user, fake_redis)

    assert {"drones/d.enc", "drones/d.mp3", "drones/d.jpg"} <= set(keys)
    assert f"drones/raw/{drone_id}.wav" in keys


@pytest.mark.asyncio
async def test_purge_does_not_repeat_a_key(db_session, fake_redis):
    user = await _make_user(db_session)
    for _ in range(2):
        db_session.add(Loop(
            id=uuid.uuid4(), title="L", slug=f"l-{uuid.uuid4().hex[:8]}",
            genre=Genre.afrobeat, bpm=100, duration=10, tempo_feel=TempoFeel.mid,
            tags=[], price=Decimal("1.00"), is_free=True, is_paid=False,
            created_by=user.id, thumbnail_s3_key="shared/thumb.jpg",
        ))
    await db_session.commit()

    keys = await _purge(db_session, user, fake_redis)

    assert keys.count("shared/thumb.jpg") == 1


@pytest.mark.asyncio
async def test_a_storage_failure_does_not_undo_the_purge(db_session, fake_redis):
    """S3 deletes run after the commit, so a bucket outage leaves an orphaned
    object — never a half-deleted account."""
    user = await _make_user(db_session)
    db_session.add(Loop(
        id=uuid.uuid4(), title="L", slug=f"l-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat, bpm=100, duration=10, tempo_feel=TempoFeel.mid,
        tags=[], price=Decimal("1.00"), is_free=True, is_paid=False,
        created_by=user.id, file_s3_key="loops/boom.enc",
    ))
    await db_session.commit()
    user_id = user.id

    async def _explode(key):
        raise RuntimeError("S3 is down")

    with patch("app.services.s3_service.delete_object", new=_explode), \
         patch("app.services.email_service.send_email", new=AsyncMock()), \
         patch(
             "app.tasks.notification_tasks.send_account_deleted_admin_notification.delay",
             new=lambda *a, **kw: None,
         ):
        await auth_service.delete_user(db_session, user, fake_redis, None)

    assert await _count(db_session, User, id=user_id) == 0
    assert await _count(db_session, Loop, created_by=user_id) == 0
