"""The genre enum's wire values are a contract with every client.

Clients send the *value* ("African Praise"), while Postgres stores the member
*name* (``african_praise``). Renaming a value silently breaks apps in the field
and renaming a member breaks the database, so both halves are pinned here.
"""
import uuid
from decimal import Decimal

import pytest

from app.models.loop import Genre, Loop, TempoFeel
from app.models.user import User, UserRole
from app.services.auth_service import hash_password


def test_wire_values_are_stable():
    assert Genre("African Praise") is Genre.african_praise
    assert Genre("Drill") is Genre.drill
    assert Genre("Seben") is Genre.seben
    assert Genre("Reggae") is Genre.reggae
    assert Genre("R&B") is Genre.rnb
    assert Genre("Afro Pop") is Genre.afro_pop
    assert Genre("Hip Hop") is Genre.hip_hop
    assert Genre("Kompa") is Genre.kompa
    assert Genre("Fuji") is Genre.fuji
    # Highlife and Highlife Gospel are separate genres, not one with a suffix.
    assert Genre("Highlife") is Genre.highlife
    assert Genre("Highlife Gospel") is Genre.highlife_gospel


def test_no_two_genres_share_a_value():
    values = [g.value for g in Genre]
    assert len(values) == len(set(values))


def test_member_names_are_valid_postgres_labels():
    # The migration writes each member name into the enum type verbatim, so a
    # name needing quoting or containing a space would break it.
    for genre in Genre:
        assert genre.name.replace("_", "").isalnum(), genre.name
        assert genre.name.islower(), genre.name


@pytest.mark.asyncio
async def test_a_new_genre_round_trips_through_the_database(db_session):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Producer",
        role=UserRole.producer,
    )
    db_session.add(user)
    await db_session.flush()

    loop = Loop(
        id=uuid.uuid4(),
        title="Seben Run",
        slug=f"seben-run-{uuid.uuid4().hex[:8]}",
        genre=Genre.seben,
        bpm=140,
        duration=30,
        tempo_feel=TempoFeel.fast,
        tags=[],
        price=Decimal("10.00"),
        created_by=user.id,
    )
    db_session.add(loop)
    await db_session.commit()

    stored = await db_session.get(Loop, loop.id)
    assert stored.genre is Genre.seben
    assert stored.genre.value == "Seben"
