"""Free-text search over the catalogue: title, description and genre in one box.

Searching used to hit the title alone, so a worship-genre loop called
"Midnight Drive" was unfindable by the word a person would actually type.
Loops and stem packs share the rules, so both are covered here.
"""
import uuid
from decimal import Decimal

import pytest

from app.models.loop import Genre, Loop, TempoFeel
from app.models.user import User, UserRole
from app.schemas.loop import LoopFilter
from app.services import loop_service
from app.services.auth_service import create_access_token, hash_password


def _token(user):
    return create_access_token(str(user.id), user.role.value)


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


async def _loop(db, user_id, *, title, genre=Genre.afrobeat, description=None, bpm=100,
                status="ready"):
    loop = Loop(
        id=uuid.uuid4(),
        title=title,
        slug=f"{uuid.uuid4().hex[:12]}",
        genre=genre,
        bpm=bpm,
        duration=30,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("10.00"),
        description=description,
        status=status,
        created_by=user_id,
    )
    db.add(loop)
    await db.commit()
    return loop


async def _titles(db, **kwargs):
    loops, _total = await loop_service.list_loops(db, LoopFilter(**kwargs))
    return sorted(l.title for l in loops)


# ── genre matching ────────────────────────────────────────────────────────────


def test_genre_matching_is_case_and_punctuation_insensitive():
    assert Genre.matching("worship") == [Genre.afrobeat_worship, Genre.contemporary_worship]
    assert Genre.matching("lofi") == [Genre.lo_fi]
    assert Genre.matching("LO-FI") == [Genre.lo_fi]
    assert Genre.matching("hip hop") == [Genre.hip_hop]
    assert Genre.matching("rb") == [Genre.rnb]
    assert Genre.matching("") == []
    assert Genre.matching("   ") == []
    assert Genre.matching("nonsense") == []


# ── search across the three fields ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_matches_the_title(db_session):
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Midnight Drive")
    await _loop(db_session, user.id, title="Morning Praise")

    assert await _titles(db_session, search="midnight") == ["Midnight Drive"]


@pytest.mark.asyncio
async def test_search_matches_the_description(db_session):
    user = await _user(db_session)
    await _loop(
        db_session, user.id, title="Untitled 4",
        description="Warm rhodes chords for a slow worship set",
    )
    await _loop(db_session, user.id, title="Other")

    assert await _titles(db_session, search="rhodes") == ["Untitled 4"]


@pytest.mark.asyncio
async def test_search_matches_the_genre(db_session):
    """The point of the change: a genre hit with nothing in the title."""
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Midnight Drive", genre=Genre.afrobeat_worship)
    await _loop(db_session, user.id, title="Trap Thing", genre=Genre.trap)

    assert await _titles(db_session, search="worship") == ["Midnight Drive"]


@pytest.mark.asyncio
async def test_genre_search_ignores_punctuation(db_session):
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Dusty Keys", genre=Genre.lo_fi)

    assert await _titles(db_session, search="lofi") == ["Dusty Keys"]


@pytest.mark.asyncio
async def test_the_three_fields_are_ored(db_session):
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Praise Break", genre=Genre.trap)
    await _loop(db_session, user.id, title="Nameless", genre=Genre.african_praise)
    await _loop(db_session, user.id, title="Notes", genre=Genre.trap,
                description="live praise recording")
    await _loop(db_session, user.id, title="Unrelated", genre=Genre.drill)

    assert await _titles(db_session, search="praise") == ["Nameless", "Notes", "Praise Break"]


@pytest.mark.asyncio
async def test_a_null_description_does_not_hide_a_title_match(db_session):
    # ILIKE against NULL is NULL, so an OR branch must not swallow the row.
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Piano Sketch", description=None)

    assert await _titles(db_session, search="piano") == ["Piano Sketch"]


# ── interaction with the other filters ────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_ands_with_the_genre_filter(db_session):
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Piano Loop", genre=Genre.trap)
    await _loop(db_session, user.id, title="Piano Loop Two", genre=Genre.gospel)

    assert await _titles(db_session, search="piano", genre=Genre.trap) == ["Piano Loop"]


@pytest.mark.asyncio
async def test_search_ands_with_the_bpm_range(db_session):
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Fast Piano", bpm=160)
    await _loop(db_session, user.id, title="Slow Piano", bpm=70)

    assert await _titles(db_session, search="piano", bpm_min=150) == ["Fast Piano"]


@pytest.mark.asyncio
async def test_the_reported_total_counts_search_hits_only(db_session):
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Piano One")
    await _loop(db_session, user.id, title="Piano Two")
    await _loop(db_session, user.id, title="Drums")

    _loops, total = await loop_service.list_loops(db_session, LoopFilter(search="piano"))

    assert total == 2


# ── stem packs use the same rules ─────────────────────────────────────────────


async def _pack(db, user_id, *, title, genre=Genre.afrobeat, description=None):
    from app.models.stem_pack import StemPack, StemPackType

    pack = StemPack(
        id=uuid.uuid4(),
        title=title,
        slug=f"{uuid.uuid4().hex[:12]}",
        genre=genre,
        pack_type=StemPackType.long_form,
        bpm=100,
        key="C",
        tags=[],
        price=Decimal("20.00"),
        description=description,
        created_by=user_id,
    )
    db.add(pack)
    await db.commit()
    return pack


async def _pack_titles(db, **kwargs):
    from app.schemas.stem_pack import StemPackFilter
    from app.services import stem_pack_service

    packs, _total = await stem_pack_service.list_stem_packs(db, StemPackFilter(**kwargs))
    return sorted(p.title for p in packs)


@pytest.mark.asyncio
async def test_pack_search_matches_title_description_and_genre(db_session):
    user = await _user(db_session)
    await _pack(db_session, user.id, title="Praise Session", genre=Genre.trap)
    await _pack(db_session, user.id, title="Nameless", genre=Genre.african_praise)
    await _pack(db_session, user.id, title="Notes", genre=Genre.trap,
                description="recorded at a praise night")
    await _pack(db_session, user.id, title="Unrelated", genre=Genre.drill)

    assert await _pack_titles(db_session, search="praise") == [
        "Nameless", "Notes", "Praise Session",
    ]


@pytest.mark.asyncio
async def test_pack_genre_search_ignores_punctuation(db_session):
    user = await _user(db_session)
    await _pack(db_session, user.id, title="Dusty Keys", genre=Genre.lo_fi)
    await _pack(db_session, user.id, title="Other", genre=Genre.trap)

    assert await _pack_titles(db_session, search="lofi") == ["Dusty Keys"]


@pytest.mark.asyncio
async def test_pack_search_ands_with_the_genre_filter(db_session):
    user = await _user(db_session)
    await _pack(db_session, user.id, title="Piano Pack", genre=Genre.trap)
    await _pack(db_session, user.id, title="Piano Pack Two", genre=Genre.gospel)

    assert await _pack_titles(db_session, search="piano", genre=Genre.trap) == ["Piano Pack"]


@pytest.mark.asyncio
async def test_a_pack_with_no_description_is_still_found_by_title(db_session):
    user = await _user(db_session)
    await _pack(db_session, user.id, title="Guitar Pack", description=None)

    assert await _pack_titles(db_session, search="guitar") == ["Guitar Pack"]


# ── drones search title, description and category name ────────────────────────


async def _category(db, user_id, name):
    from app.models.drone_pad import DronePadCategory

    category = DronePadCategory(id=uuid.uuid4(), name=name, created_by=user_id)
    db.add(category)
    await db.flush()
    return category


async def _drone(db, user_id, *, title, description=None, category=None):
    from app.models.drone_pad import Drone

    drone = Drone(
        id=uuid.uuid4(),
        title=title,
        description=description,
        price=Decimal("0.00"),
        is_free=True,
        category_id=category.id if category else None,
        created_by=user_id,
    )
    db.add(drone)
    await db.commit()
    return drone


async def _drone_titles(db, **kwargs):
    from app.schemas.drone_pad import DronePadFilter
    from app.services import drone_service

    drones, _total = await drone_service.list_drones(db, DronePadFilter(**kwargs))
    return sorted(d.title for d in drones)


@pytest.mark.asyncio
async def test_drone_search_matches_the_title(db_session):
    user = await _user(db_session)
    await _drone(db_session, user.id, title="Deep Space Pad")
    await _drone(db_session, user.id, title="Warm Strings")

    assert await _drone_titles(db_session, search="space") == ["Deep Space Pad"]


@pytest.mark.asyncio
async def test_drone_search_matches_the_description(db_session):
    user = await _user(db_session)
    await _drone(db_session, user.id, title="Untitled", description="swelling cinematic bed")
    await _drone(db_session, user.id, title="Other")

    assert await _drone_titles(db_session, search="cinematic") == ["Untitled"]


@pytest.mark.asyncio
async def test_drone_search_matches_the_category_name(db_session):
    """The point of the change: a category hit with nothing in the title."""
    user = await _user(db_session)
    cinematic = await _category(db_session, user.id, "Cinematic")
    await _drone(db_session, user.id, title="Deep Space Pad", category=cinematic)
    await _drone(db_session, user.id, title="Warm Strings")

    assert await _drone_titles(db_session, search="cinematic") == ["Deep Space Pad"]


@pytest.mark.asyncio
async def test_a_drone_with_no_category_is_still_found_by_title(db_session):
    # The category branch is an EXISTS — it must not exclude uncategorised rows.
    user = await _user(db_session)
    await _drone(db_session, user.id, title="Lonely Pad", category=None)

    assert await _drone_titles(db_session, search="lonely") == ["Lonely Pad"]


@pytest.mark.asyncio
async def test_drone_search_returns_each_drone_once(db_session):
    """A join would duplicate a row matching on both its own field and its
    category; EXISTS keeps it to one."""
    user = await _user(db_session)
    ambient = await _category(db_session, user.id, "Ambient")
    await _drone(db_session, user.id, title="Ambient Pad",
                 description="ambient texture", category=ambient)

    from app.schemas.drone_pad import DronePadFilter
    from app.services import drone_service

    drones, total = await drone_service.list_drones(
        db_session, DronePadFilter(search="ambient")
    )

    assert [d.title for d in drones] == ["Ambient Pad"]
    assert total == 1


@pytest.mark.asyncio
async def test_drone_search_ands_with_the_category_filter(db_session):
    user = await _user(db_session)
    cinematic = await _category(db_session, user.id, "Cinematic")
    ambient = await _category(db_session, user.id, "Ambient")
    await _drone(db_session, user.id, title="Pad One", category=cinematic)
    await _drone(db_session, user.id, title="Pad Two", category=ambient)

    assert await _drone_titles(
        db_session, search="pad", category_id=cinematic.id
    ) == ["Pad One"]


# ── the public catalogue only shows loops that can actually be played ─────────


@pytest.mark.asyncio
async def test_public_listing_hides_loops_that_are_not_ready(db_session):
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Playable", status="ready")
    await _loop(db_session, user.id, title="Still Encoding", status="processing")
    await _loop(db_session, user.id, title="Broken Upload", status="failed")

    assert await _titles(db_session, ready_only=True) == ["Playable"]


@pytest.mark.asyncio
async def test_the_public_total_excludes_unready_loops(db_session):
    """The count drives pagination, so it has to match what is listed."""
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Playable", status="ready")
    await _loop(db_session, user.id, title="Hidden", status="processing")

    _loops, total = await loop_service.list_loops(db_session, LoopFilter(ready_only=True))

    assert total == 1


@pytest.mark.asyncio
async def test_a_producer_still_sees_their_own_uploads_processing(db_session):
    """Hiding them from the producer would remove the only view of an upload
    in flight — the whole point of the status they poll."""
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Playable", status="ready")
    await _loop(db_session, user.id, title="Still Encoding", status="processing")

    assert await _titles(db_session, created_by=user.id) == ["Playable", "Still Encoding"]


@pytest.mark.asyncio
async def test_ready_only_ands_with_search(db_session):
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Piano Ready", status="ready")
    await _loop(db_session, user.id, title="Piano Processing", status="processing")

    assert await _titles(db_session, ready_only=True, search="piano") == ["Piano Ready"]


# ── the same readiness rule for kits, drones and packs ────────────────────────


@pytest.mark.asyncio
async def test_public_listing_hides_a_kit_with_unfinished_samples(db_session):
    from app.models.drum_kit import DrumKit, DrumSample
    from app.schemas.drum_kit import DrumKitFilter
    from app.services import drum_kit_service

    user = await _user(db_session)

    async def _kit(title, statuses):
        kit = DrumKit(
            id=uuid.uuid4(), title=title, slug=uuid.uuid4().hex[:12],
            tags=[], is_free=True, created_by=user.id,
        )
        db_session.add(kit)
        await db_session.flush()
        for status in statuses:
            db_session.add(DrumSample(
                id=uuid.uuid4(), drum_kit_id=kit.id, label="Kick",
                status=status, duration=1,
            ))
        await db_session.commit()

    await _kit("Ready Kit", ("ready", "ready"))
    await _kit("Half Kit", ("ready", "processing"))
    await _kit("Empty Kit", ())

    kits, total = await drum_kit_service.list_drum_kits(
        db_session, DrumKitFilter(ready_only=True)
    )

    assert [k.title for k in kits] == ["Ready Kit"]
    assert total == 1


@pytest.mark.asyncio
async def test_public_listing_hides_a_drone_with_a_failed_pad(db_session):
    from app.models.drone_pad import Drone, DronePad, MusicalKey
    from app.schemas.drone_pad import DronePadFilter
    from app.services import drone_service

    user = await _user(db_session)

    async def _drone_with(title, statuses):
        drone = Drone(
            id=uuid.uuid4(), title=title, price=Decimal("0.00"),
            is_free=True, created_by=user.id,
        )
        db_session.add(drone)
        await db_session.flush()
        for status in statuses:
            db_session.add(DronePad(
                id=uuid.uuid4(), drone_id=drone.id, key=MusicalKey.C,
                duration=30, status=status,
            ))
        await db_session.commit()

    await _drone_with("Good Drone", ("ready",))
    await _drone_with("Broken Drone", ("ready", "failed"))
    await _drone_with("Empty Drone", ())

    drones, total = await drone_service.list_drones(
        db_session, DronePadFilter(ready_only=True)
    )

    assert [d.title for d in drones] == ["Good Drone"]
    assert total == 1


@pytest.mark.asyncio
async def test_public_listing_hides_a_pack_with_unfinished_stems(db_session):
    from app.models.stem_pack import Stem, StemInstrument, StemPack, StemPackType
    from app.schemas.stem_pack import StemPackFilter
    from app.services import stem_pack_service

    user = await _user(db_session)

    async def _pack_with(title, statuses):
        pack = StemPack(
            id=uuid.uuid4(), title=title, slug=uuid.uuid4().hex[:12],
            genre=Genre.afrobeat, pack_type=StemPackType.long_form,
            bpm=100, key="C", tags=[], price=Decimal("20.00"), created_by=user.id,
        )
        db_session.add(pack)
        await db_session.flush()
        for i, status in enumerate(statuses):
            db_session.add(Stem(
                id=uuid.uuid4(), stem_pack_id=pack.id,
                instrument=list(StemInstrument)[i], label="Stem", status=status,
            ))
        await db_session.commit()

    await _pack_with("Ready Pack", ("ready", "ready"))
    await _pack_with("Half Pack", ("ready", "processing"))
    await _pack_with("Empty Pack", ())

    packs, total = await stem_pack_service.list_stem_packs(
        db_session, StemPackFilter(ready_only=True)
    )

    assert [p.title for p in packs] == ["Ready Pack"]
    assert total == 1


@pytest.mark.asyncio
async def test_producers_still_see_their_own_unfinished_uploads(db_session):
    """Same split as loops: the producer view is where an upload in flight is
    tracked, so readiness must not be filtered there."""
    from app.models.drum_kit import DrumKit, DrumSample
    from app.schemas.drum_kit import DrumKitFilter
    from app.services import drum_kit_service

    user = await _user(db_session)
    kit = DrumKit(
        id=uuid.uuid4(), title="Half Kit", slug=uuid.uuid4().hex[:12],
        tags=[], is_free=True, created_by=user.id,
    )
    db_session.add(kit)
    await db_session.flush()
    db_session.add(DrumSample(
        id=uuid.uuid4(), drum_kit_id=kit.id, label="Kick",
        status="processing", duration=1,
    ))
    await db_session.commit()

    kits, _total = await drum_kit_service.list_drum_kits(
        db_session, DrumKitFilter(created_by=user.id)
    )

    assert [k.title for k in kits] == ["Half Kit"]


# ── pagination cannot be used to ask for the whole table ──────────────────────


def test_loop_pagination_is_bounded():
    from app.schemas.loop import LoopFilter

    assert LoopFilter(page_size=100_000).page_size == 100
    assert LoopFilter(page_size=0).page_size == 1
    # page 0 would build OFFSET -20, which Postgres refuses outright.
    assert LoopFilter(page=0).page == 1
    assert LoopFilter(page=-5).page == 1
    # Ordinary values are untouched.
    assert LoopFilter(page=3, page_size=50).page == 3
    assert LoopFilter(page=3, page_size=50).page_size == 50


def test_drone_pagination_is_bounded():
    from app.schemas.drone_pad import DronePadFilter

    assert DronePadFilter(page_size=100_000).page_size == 100
    assert DronePadFilter(page_size=0).page_size == 1
    assert DronePadFilter(page=0).page == 1
    assert DronePadFilter(page=-5).page == 1
    assert DronePadFilter(page=2, page_size=50).page_size == 50


@pytest.mark.asyncio
async def test_a_zero_page_no_longer_500s(client, db_session):
    """It used to reach Postgres as OFFSET -20 and blow up."""
    user = await _user(db_session)
    await _loop(db_session, user.id, title="Anything")

    resp = await client.get("/api/v1/loops?page=0", headers={
        "Authorization": f"Bearer {_token(user)}"
    })

    assert resp.status_code == 200
    assert resp.json()["data"]["items"]
