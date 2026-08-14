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


async def _loop(db, user_id, *, title, genre=Genre.afrobeat, description=None, bpm=100):
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
