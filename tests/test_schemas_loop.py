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
