from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.loop import Genre, TempoFeel
from app.schemas.loop import LoopCreate, LoopUpdate


def test_loop_create_defaults_time_signature():
    loop = LoopCreate(
        title="Test Loop",
        genre=Genre.afrobeat,
        bpm=100,
        tempo_feel=TempoFeel.mid,
        price=Decimal("4.99"),
    )

    assert loop.time_signature == "4/4"


def test_loop_create_accepts_time_signature():
    loop = LoopCreate(
        title="Test Loop",
        genre=Genre.afrobeat,
        bpm=100,
        time_signature="6/8",
        tempo_feel=TempoFeel.mid,
        price=Decimal("4.99"),
    )

    assert loop.time_signature == "6/8"


def test_loop_create_rejects_invalid_time_signature():
    with pytest.raises(ValidationError):
        LoopCreate(
            title="Test Loop",
            genre=Genre.afrobeat,
            bpm=100,
            time_signature="common",
            tempo_feel=TempoFeel.mid,
            price=Decimal("4.99"),
        )


def test_loop_update_accepts_time_signature():
    update = LoopUpdate(time_signature="3/4")

    assert update.time_signature == "3/4"


def _loop(**overrides):
    fields = dict(
        title="Test Loop",
        genre=Genre.afrobeat,
        bpm=100,
        tempo_feel=TempoFeel.mid,
        price=Decimal("4.99"),
    )
    fields.update(overrides)
    return LoopCreate(**fields)


def test_loop_create_accepts_the_fast_end_of_the_catalogue():
    # Drill lands near 145 and seben past 150; the old 140 ceiling refused both.
    assert _loop(bpm=145).bpm == 145
    assert _loop(bpm=250).bpm == 250


def test_loop_create_accepts_the_slow_end_of_the_catalogue():
    # Half-time trap counts near 60-70 and downtempo beds sit below that; the
    # old 60 floor refused everything under it.
    assert _loop(bpm=40).bpm == 40
    assert _loop(bpm=59).bpm == 59


def test_loop_create_rejects_bpm_above_the_ceiling():
    with pytest.raises(ValidationError) as excinfo:
        _loop(bpm=251)
    assert "between 40 and 250" in str(excinfo.value)


def test_loop_create_still_rejects_bpm_below_the_floor():
    with pytest.raises(ValidationError) as excinfo:
        _loop(bpm=39)
    assert "between 40 and 250" in str(excinfo.value)


def test_loop_update_shares_the_same_bpm_range():
    assert LoopUpdate(bpm=40).bpm == 40
    assert LoopUpdate(bpm=250).bpm == 250
    with pytest.raises(ValidationError):
        LoopUpdate(bpm=39)
    with pytest.raises(ValidationError):
        LoopUpdate(bpm=251)
