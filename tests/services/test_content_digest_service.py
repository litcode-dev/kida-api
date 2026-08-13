"""The digest's two jobs: pick the right items, and claim them exactly once."""
import uuid
from decimal import Decimal

import pytest

from app.models.drone_pad import Drone, DronePad, MusicalKey
from app.models.drum_kit import DrumKit, DrumSample
from app.models.loop import Genre, Loop, TempoFeel
from app.models.stem_pack import Stem, StemInstrument, StemPack, StemPackType
from app.models.user import User, UserRole
from app.services import content_digest_service
from app.services.auth_service import hash_password


async def _user(db):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Producer",
        role=UserRole.producer,
    )
    db.add(user)
    await db.flush()
    return user


async def _loop(db, user_id, *, status="ready", announced=False, title="Loop"):
    loop = Loop(
        id=uuid.uuid4(),
        title=title,
        slug=f"{title.lower()}-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        bpm=100,
        duration=30,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("10.00"),
        status=status,
        created_by=user_id,
    )
    if announced:
        from datetime import datetime, timezone
        loop.announced_at = datetime.now(timezone.utc)
    db.add(loop)
    await db.commit()
    return loop


async def _drum_kit(db, user_id, *, sample_statuses=("ready",), title="Kit"):
    kit = DrumKit(
        id=uuid.uuid4(),
        title=title,
        slug=f"{title.lower()}-{uuid.uuid4().hex[:8]}",
        tags=[],
        is_free=True,
        created_by=user_id,
    )
    db.add(kit)
    await db.flush()
    for status in sample_statuses:
        db.add(DrumSample(
            id=uuid.uuid4(), drum_kit_id=kit.id, label="Kick", status=status, duration=1
        ))
    await db.commit()
    return kit


async def _drone(db, user_id, *, pad_statuses=("ready",), title="Drone"):
    drone = Drone(
        id=uuid.uuid4(), title=title, price=Decimal("0.00"), is_free=True, created_by=user_id
    )
    db.add(drone)
    await db.flush()
    for status in pad_statuses:
        db.add(DronePad(
            id=uuid.uuid4(), drone_id=drone.id, key=MusicalKey.C, duration=30, status=status
        ))
    await db.commit()
    return drone


async def _stem_pack(db, user_id, *, published=True, stem_statuses=("ready",), title="Pack"):
    from datetime import datetime, timezone

    pack = StemPack(
        id=uuid.uuid4(),
        title=title,
        slug=f"{title.lower()}-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        pack_type=StemPackType.long_form,
        bpm=100,
        key="C",
        tags=[],
        price=Decimal("20.00"),
        created_by=user_id,
        published_at=datetime.now(timezone.utc) if published else None,
    )
    db.add(pack)
    await db.flush()
    for i, status in enumerate(stem_statuses):
        db.add(Stem(
            id=uuid.uuid4(),
            stem_pack_id=pack.id,
            instrument=list(StemInstrument)[i],
            label="Stem",
            status=status,
        ))
    await db.commit()
    return pack


@pytest.mark.asyncio
async def test_collects_only_ready_and_unannounced(db_session):
    user = await _user(db_session)
    ready = await _loop(db_session, user.id, title="Ready")
    await _loop(db_session, user.id, status="processing", title="Processing")
    await _loop(db_session, user.id, announced=True, title="Already")

    digest, claims = await content_digest_service.collect(db_session)

    assert [i.title for i in digest.items["loop"]] == ["Ready"]
    assert claims["loop"] == [ready.id]


@pytest.mark.asyncio
async def test_a_kit_waits_for_every_sample(db_session):
    user = await _user(db_session)
    await _drum_kit(db_session, user.id, sample_statuses=("ready", "processing"), title="Half")
    await _drum_kit(db_session, user.id, sample_statuses=("ready", "ready"), title="Done")

    digest, _ = await content_digest_service.collect(db_session)

    assert [i.title for i in digest.items["drum_kit"]] == ["Done"]


@pytest.mark.asyncio
async def test_an_empty_kit_is_not_announced(db_session):
    # A kit row with no samples is a half-made upload, not a release.
    user = await _user(db_session)
    await _drum_kit(db_session, user.id, sample_statuses=(), title="Empty")

    digest, _ = await content_digest_service.collect(db_session)

    assert digest.items["drum_kit"] == []


@pytest.mark.asyncio
async def test_a_drone_waits_for_every_pad(db_session):
    user = await _user(db_session)
    await _drone(db_session, user.id, pad_statuses=("ready", "failed"), title="Broken")
    await _drone(db_session, user.id, pad_statuses=("ready",), title="Good")

    digest, _ = await content_digest_service.collect(db_session)

    assert [i.title for i in digest.items["drone_pad"]] == ["Good"]


@pytest.mark.asyncio
async def test_an_unpublished_pack_is_held_back(db_session):
    # Unlike the other types, a stem pack is only announced once its producer
    # publishes it — a half-filled breakdown pack is not a release.
    user = await _user(db_session)
    await _stem_pack(db_session, user.id, published=False, title="Draft")
    live = await _stem_pack(db_session, user.id, published=True, title="Live")

    digest, claims = await content_digest_service.collect(db_session)

    assert [i.title for i in digest.items["stem_pack"]] == ["Live"]
    assert claims["stem_pack"] == [live.id]


@pytest.mark.asyncio
async def test_claiming_makes_the_next_digest_empty(db_session):
    user = await _user(db_session)
    await _loop(db_session, user.id, title="One")
    await _drum_kit(db_session, user.id, title="Two")

    digest, claims = await content_digest_service.collect(db_session)
    assert digest.total == 2

    claimed = await content_digest_service.claim(db_session, claims)
    assert claimed == 2

    again, _ = await content_digest_service.collect(db_session)
    assert again.is_empty()


@pytest.mark.asyncio
async def test_overflow_items_are_still_claimed(db_session):
    """Items trimmed from the email must not reappear tomorrow.

    They were announced — just not named individually — so the claim covers
    every eligible item, not only the ones that fitted.
    """
    user = await _user(db_session)
    limit = content_digest_service.MAX_ITEMS_PER_SECTION
    for i in range(limit + 3):
        await _loop(db_session, user.id, title=f"Loop{i}")

    digest, claims = await content_digest_service.collect(db_session)

    assert len(digest.items["loop"]) == limit
    assert len(claims["loop"]) == limit + 3

    await content_digest_service.claim(db_session, claims)
    again, _ = await content_digest_service.collect(db_session)
    assert again.is_empty()


@pytest.mark.asyncio
async def test_sections_skip_empty_groups_and_keep_order(db_session):
    user = await _user(db_session)
    await _loop(db_session, user.id, title="A Loop")
    await _drone(db_session, user.id, title="A Drone")

    digest, _ = await content_digest_service.collect(db_session)
    keys = [key for key, _label, _items in digest.sections()]

    assert keys == ["loop", "drone_pad"]
