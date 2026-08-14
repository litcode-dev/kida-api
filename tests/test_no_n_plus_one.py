"""Query-count guards: a listing must not get chattier as it returns more rows.

An N+1 is invisible in a test that seeds one row — the extra query per item is
the same query the single-row case already runs. So every case here renders the
same endpoint twice, once with one item and once with several, and asserts the
statement count is *identical*. Anything that grows with the page is an N+1,
whatever it looks like in the source.

The counter taps ``before_cursor_execute`` on the test engine, so it sees the
real statements — lazy loads, selectin follow-ups, count queries and all. It
does not see reads answered from the session identity map, which by definition
never reach the database.
"""
import contextlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.models.download import Download
from app.models.drone_pad import Drone, DronePad, DronePadCategory, MusicalKey
from app.models.drum_kit import DrumKit, DrumSample
from app.models.like import Like
from app.models.loop import Genre, Loop, TempoFeel
from app.models.newsletter import NewsletterSubscriber
from app.models.stem_pack import (
    SongSection,
    Stem,
    StemArrangement,
    StemArrangementItem,
    StemInstrument,
    StemPack,
    StemPackType,
    StemPart,
)
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password
from tests.conftest import test_engine


@contextlib.contextmanager
def count_queries():
    """Collect every SQL statement the test engine executes inside the block."""
    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", before)


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    """Measure the database path, not Redis.

    Both the drum-kit and drone listings are cache-aside; a warm entry would
    answer with zero queries and hide whatever the fetch actually does.
    """
    from app.services import cache_service

    async def straight_through(key, fetch_fn, ttl):
        return await fetch_fn()

    monkeypatch.setattr(cache_service, "get_or_set", straight_through)


async def _user(db, role=UserRole.user):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Test",
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


# ── seeding ───────────────────────────────────────────────────────────────────
#
# Each seeder adds `count` more of its resource, so a caller can grow a
# catalogue in steps and measure at each size.


async def _seed_loops(db, owner, count):
    loops = []
    for i in range(count):
        loop = Loop(
            id=uuid.uuid4(), title=f"Loop {i}", slug=f"loop-{uuid.uuid4().hex[:10]}",
            genre=Genre.afrobeat, bpm=100, key="C major", duration=30,
            tempo_feel=TempoFeel.mid, tags=["test"], price=Decimal("4.99"),
            is_free=True, status="ready", created_by=owner.id,
        )
        db.add(loop)
        loops.append(loop)
    await db.commit()
    return loops


async def _seed_drum_kits(db, owner, count, samples_each=3):
    for i in range(count):
        kit = DrumKit(
            id=uuid.uuid4(), title=f"Kit {i}", slug=f"kit-{uuid.uuid4().hex[:10]}",
            tags=["test"], is_free=True, created_by=owner.id,
        )
        kit.samples = [
            DrumSample(id=uuid.uuid4(), label=f"Kick {n}", duration=2, status="ready")
            for n in range(samples_each)
        ]
        db.add(kit)
    await db.commit()


async def _seed_drones(db, owner, count, pads_each=3):
    category = DronePadCategory(
        id=uuid.uuid4(), name=f"Cinematic {uuid.uuid4().hex[:6]}", created_by=owner.id
    )
    db.add(category)
    keys = list(MusicalKey)
    for i in range(count):
        drone = Drone(
            id=uuid.uuid4(), title=f"Drone {i}", is_free=True,
            category_id=category.id, created_by=owner.id,
        )
        drone.pads = [
            DronePad(id=uuid.uuid4(), key=keys[n], duration=60, status="ready")
            for n in range(pads_each)
        ]
        db.add(drone)
    await db.commit()


async def _seed_stem_packs(db, owner, count, parts_each=2, instruments_each=2):
    instruments = [StemInstrument.drums, StemInstrument.piano][:instruments_each]
    packs = []
    for i in range(count):
        pack = StemPack(
            id=uuid.uuid4(), title=f"Pack {i}", slug=f"pack-{uuid.uuid4().hex[:10]}",
            genre=Genre.gospel, pack_type=StemPackType.breakdown, bpm=90, key="C",
            tags=["test"], price=Decimal("19.99"), created_by=owner.id,
        )
        pack.parts = [
            StemPart(
                id=uuid.uuid4(), section=SongSection.verse, name=f"Verse {n + 1}",
                position=n, bars=8,
                stems=[
                    Stem(
                        id=uuid.uuid4(), stem_pack_id=pack.id, instrument=inst,
                        label=inst.value, duration=30, status="ready",
                    )
                    for inst in instruments
                ],
            )
            for n in range(parts_each)
        ]
        pack.arrangements = [
            StemArrangement(
                id=uuid.uuid4(), name="Full Song", is_default=True,
                status="draft", created_by=owner.id,
            )
        ]
        db.add(pack)
        await db.flush()
        pack.arrangements[0].items = [
            StemArrangementItem(
                id=uuid.uuid4(), part_id=part.id, position=n, repeat_count=1
            )
            for n, part in enumerate(pack.parts)
        ]
        packs.append(pack)
    await db.commit()
    return packs


async def _seed_liked_loops(db, owner, count):
    for loop in await _seed_loops(db, owner, count):
        db.add(Like(id=uuid.uuid4(), user_id=owner.id, loop_id=loop.id))
    await db.commit()


async def _seed_liked_stem_packs(db, owner, count):
    for pack in await _seed_stem_packs(db, owner, count):
        db.add(Like(id=uuid.uuid4(), user_id=owner.id, stem_pack_id=pack.id))
    await db.commit()


async def _seed_downloads(db, owner, count):
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    for loop in await _seed_loops(db, owner, count):
        db.add(Download(
            id=uuid.uuid4(), user_id=owner.id, loop_id=loop.id,
            download_url="https://example.test/x", expires_at=expires,
        ))
    await db.commit()


async def _seed_users(db, owner, count):
    for _ in range(count):
        await _user(db)


async def _seed_subscribers(db, owner, count):
    for _ in range(count):
        db.add(NewsletterSubscriber(id=uuid.uuid4(), email=f"{uuid.uuid4()}@test.com"))
    await db.commit()


# ── the harness ───────────────────────────────────────────────────────────────

SMALL, LARGE = 1, 6


def _items(payload):
    data = payload["data"]
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Most listings say "items"; the subscriber list names its own rows.
        for key in ("items", "subscribers"):
            if key in data:
                return data[key]
    return []


async def _query_counts(client, db, user, url, seed):
    """Render ``url`` at two catalogue sizes and return both query counts."""
    await seed(db, user, SMALL)
    with count_queries() as first:
        resp = await client.get(url, headers=_headers(user))
        assert resp.status_code == 200, resp.text
    small_items = len(_items(resp.json()))

    await seed(db, user, LARGE - SMALL)
    with count_queries() as second:
        resp = await client.get(url, headers=_headers(user))
        assert resp.status_code == 200, resp.text
    large_items = len(_items(resp.json()))

    # A listing that did not actually grow proves nothing about N+1.
    assert large_items > small_items, (
        f"{url}: seeding did not grow the response ({small_items} → {large_items})"
    )
    return len(first), len(second)


# url, role, seeder
CASES = [
    ("/api/v1/loops", UserRole.user, _seed_loops),
    ("/api/v1/drum-kits", UserRole.user, _seed_drum_kits),
    ("/api/v1/drones", UserRole.user, _seed_drones),
    ("/api/v1/stem-packs", UserRole.user, _seed_stem_packs),
    ("/api/v1/likes/loops", UserRole.user, _seed_liked_loops),
    ("/api/v1/likes/stem-packs", UserRole.user, _seed_liked_stem_packs),
    ("/api/v1/downloads", UserRole.user, _seed_downloads),
    ("/api/v1/producer/loops", UserRole.producer, _seed_loops),
    ("/api/v1/producer/drones", UserRole.producer, _seed_drones),
    ("/api/v1/producer/drum-kits", UserRole.producer, _seed_drum_kits),
    ("/api/v1/producer/stem-packs", UserRole.producer, _seed_stem_packs),
    ("/api/v1/admin/drum-kits", UserRole.admin, _seed_drum_kits),
    ("/api/v1/admin/stem-packs", UserRole.admin, _seed_stem_packs),
    ("/api/v1/admin/users", UserRole.admin, _seed_users),
    ("/api/v1/admin/newsletter/subscribers", UserRole.admin, _seed_subscribers),
]


@pytest.mark.parametrize("url,role,seed", CASES, ids=[c[0] for c in CASES])
@pytest.mark.asyncio
async def test_listing_query_count_does_not_grow_with_the_page(
    client, db_session, url, role, seed
):
    user = await _user(db_session, role)
    one, many = await _query_counts(client, db_session, user, url, seed)
    assert one == many, (
        f"{url}: {one} queries for {SMALL} item, {many} for {LARGE} — "
        "the listing issues a query per row"
    )


# ── depth, not just breadth ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stem_pack_listing_is_flat_in_parts_and_stems(client, db_session):
    """A pack with more parts, each with more instruments, must not cost a
    query per part or per stem — nesting is where eager loading usually stops
    one level short."""
    user = await _user(db_session)

    await _seed_stem_packs(db_session, user, 1, parts_each=1, instruments_each=1)
    with count_queries() as shallow:
        resp = await client.get("/api/v1/stem-packs", headers=_headers(user))
        assert resp.status_code == 200, resp.text

    await _seed_stem_packs(db_session, user, 1, parts_each=2, instruments_each=2)
    with count_queries() as deep:
        resp = await client.get("/api/v1/stem-packs", headers=_headers(user))
        assert resp.status_code == 200, resp.text

    assert len(shallow) == len(deep), (
        f"stem packs: {len(shallow)} queries for a 1-part pack, "
        f"{len(deep)} once parts and stems multiply"
    )


@pytest.mark.asyncio
async def test_stem_pack_arrangements_are_flat_in_items(client, db_session):
    """Each arrangement item points at a part; resolving those one at a time
    is the N+1 this endpoint is most exposed to."""
    user = await _user(db_session)

    (small,) = await _seed_stem_packs(db_session, user, 1, parts_each=1)
    with count_queries() as few:
        resp = await client.get(
            f"/api/v1/stem-packs/{small.id}/arrangements", headers=_headers(user)
        )
        assert resp.status_code == 200, resp.text

    (big,) = await _seed_stem_packs(db_session, user, 1, parts_each=4)
    with count_queries() as many:
        resp = await client.get(
            f"/api/v1/stem-packs/{big.id}/arrangements", headers=_headers(user)
        )
        assert resp.status_code == 200, resp.text

    assert len(few) == len(many), (
        f"arrangements: {len(few)} queries for 1 item, {len(many)} for 4"
    )


@pytest.mark.asyncio
async def test_stem_pack_detail_is_flat_in_parts_and_stems(client, db_session):
    user = await _user(db_session)

    (small,) = await _seed_stem_packs(db_session, user, 1, parts_each=1, instruments_each=1)
    with count_queries() as few:
        resp = await client.get(f"/api/v1/stem-packs/{small.id}", headers=_headers(user))
        assert resp.status_code == 200, resp.text

    (big,) = await _seed_stem_packs(db_session, user, 1, parts_each=2, instruments_each=2)
    with count_queries() as many:
        resp = await client.get(f"/api/v1/stem-packs/{big.id}", headers=_headers(user))
        assert resp.status_code == 200, resp.text

    assert len(few) == len(many), (
        f"stem pack detail: {len(few)} queries for a 1-part pack, {len(many)} for a 2×2"
    )


@pytest.mark.asyncio
async def test_drum_kit_detail_is_flat_in_samples(client, db_session):
    user = await _user(db_session)

    await _seed_drum_kits(db_session, user, 1, samples_each=1)
    await _seed_drum_kits(db_session, user, 1, samples_each=8)
    kits = (await db_session.execute(select(DrumKit).order_by(DrumKit.title))).scalars().all()
    small, big = kits[0], kits[1]

    with count_queries() as few:
        resp = await client.get(f"/api/v1/drum-kits/{small.id}", headers=_headers(user))
        assert resp.status_code == 200, resp.text
    with count_queries() as many:
        resp = await client.get(f"/api/v1/drum-kits/{big.id}", headers=_headers(user))
        assert resp.status_code == 200, resp.text

    assert len(few) == len(many), (
        f"drum kit detail: {len(few)} queries for 1 sample, {len(many)} for 8"
    )


# ── writes ────────────────────────────────────────────────────────────────────


async def _upload_stems(db, pack, instruments, part_id=None):
    from unittest.mock import AsyncMock, patch

    from app.schemas.stem_pack import StemCreate
    from app.services import stem_pack_service

    files = [AsyncMock() for _ in instruments]
    data = [StemCreate(instrument=i, part_id=part_id) for i in instruments]
    with (
        patch(
            "app.services.stem_pack_service.validate_wav_upload",
            new=AsyncMock(return_value=b"RIFF"),
        ),
        patch(
            "app.services.stem_pack_service.s3_service.upload_bytes",
            new=AsyncMock(return_value="key"),
        ),
    ):
        return await stem_pack_service.add_stems_to_pack(db, pack.id, files, data)


async def _long_form_pack(db, owner):
    pack = StemPack(
        id=uuid.uuid4(), title="LF", slug=f"lf-{uuid.uuid4().hex[:10]}",
        genre=Genre.gospel, pack_type=StemPackType.long_form, bpm=90, key="C",
        tags=[], price=Decimal("10.00"), created_by=owner.id,
    )
    db.add(pack)
    await db.commit()
    return pack


@pytest.mark.asyncio
async def test_multi_stem_upload_query_count_is_flat(db_session):
    """Uploading eight instruments used to cost a "is this instrument taken?"
    query and a refresh per file — 2N+1 round trips for one request."""
    user = await _user(db_session, UserRole.producer)
    instruments = list(StemInstrument)

    one_pack = await _long_form_pack(db_session, user)
    with count_queries() as few:
        await _upload_stems(db_session, one_pack, instruments[:1])

    many_pack = await _long_form_pack(db_session, user)
    with count_queries() as many:
        await _upload_stems(db_session, many_pack, instruments[:4])

    assert len(few) == len(many), (
        f"stem upload: {len(few)} queries for 1 stem, {len(many)} for 4"
    )


@pytest.mark.asyncio
async def test_multi_stem_upload_still_returns_server_defaults(db_session):
    """The per-stem refresh loop existed to pull back server-side defaults;
    whatever replaces it has to populate them just the same."""
    user = await _user(db_session, UserRole.producer)
    pack = await _long_form_pack(db_session, user)

    created = await _upload_stems(db_session, pack, list(StemInstrument)[:3])

    assert len(created) == 3
    for stem in created:
        assert stem.created_at is not None, "created_at was never loaded from the database"
        assert stem.status == "processing"
        assert stem.duration == 0


@pytest.mark.asyncio
async def test_upload_still_rejects_an_instrument_already_in_the_pack(db_session):
    """The batched slot lookup replaced a per-stem SELECT; the 409 it produced
    has to survive on both the long-form and the breakdown branch."""
    from app.exceptions import ConflictError

    user = await _user(db_session, UserRole.producer)

    long_form = await _long_form_pack(db_session, user)
    await _upload_stems(db_session, long_form, [StemInstrument.drums])
    with pytest.raises(ConflictError, match="this pack"):
        await _upload_stems(db_session, long_form, [StemInstrument.drums])

    (breakdown,) = await _seed_stem_packs(db_session, user, 1, parts_each=1, instruments_each=1)
    part = breakdown.parts[0]
    with pytest.raises(ConflictError, match="this part"):
        await _upload_stems(db_session, breakdown, [StemInstrument.drums], part_id=part.id)

    # A different part is a different slot, so the same instrument is free there.
    other = StemPart(
        id=uuid.uuid4(), stem_pack_id=breakdown.id, section=SongSection.chorus,
        name="Chorus", position=9, bars=8,
    )
    db_session.add(other)
    await db_session.commit()
    assert await _upload_stems(db_session, breakdown, [StemInstrument.drums], part_id=other.id)


# ── the digest, which reads every catalogue at once ───────────────────────────


@pytest.mark.asyncio
async def test_content_digest_collection_is_flat(db_session):
    """The digest sweeps four catalogues on a schedule; a per-item query here
    would be the most expensive N+1 in the system, since nobody is watching a
    request clock when it runs."""
    from app.services import content_digest_service

    user = await _user(db_session)

    await _seed_loops(db_session, user, 1)
    await _seed_drum_kits(db_session, user, 1)
    await _seed_drones(db_session, user, 1)
    with count_queries() as few:
        await content_digest_service.collect(db_session)

    await _seed_loops(db_session, user, 5)
    await _seed_drum_kits(db_session, user, 5)
    await _seed_drones(db_session, user, 5)
    with count_queries() as many:
        digest, _claim_ids = await content_digest_service.collect(db_session)

    assert digest.total > 3, "digest collected nothing, so the count proves nothing"
    assert len(few) == len(many), (
        f"digest collection: {len(few)} queries for 3 items, {len(many)} for 18"
    )
