"""The handler-built-model path: a bad value is the caller's 422, not our 500."""
import pytest
from pydantic import BaseModel, field_validator

from app.exceptions import AppError, parse_model, validation_error_422


class _Model(BaseModel):
    bpm: int
    label: str = "x"

    @field_validator("bpm")
    @classmethod
    def in_range(cls, v: int) -> int:
        if not 60 <= v <= 250:
            raise ValueError("BPM must be between 60 and 250")
        return v


def test_parse_model_returns_the_model_when_valid():
    assert parse_model(_Model, bpm=90).bpm == 90


def test_parse_model_raises_app_error_422_on_a_failed_validator():
    with pytest.raises(AppError) as exc_info:
        parse_model(_Model, bpm=0)

    assert exc_info.value.status_code == 422
    # The validator's own wording, not pydantic's "Value error, ..." wrapper.
    assert exc_info.value.message == "bpm: BPM must be between 60 and 250"


def test_parse_model_reports_every_bad_field():
    with pytest.raises(AppError) as exc_info:
        parse_model(_Model, bpm=0, label=None)

    message = exc_info.value.message
    assert "bpm: BPM must be between 60 and 250" in message
    assert "label" in message


def test_validation_error_422_names_nested_fields():
    class _Outer(BaseModel):
        inner: _Model

    try:
        _Outer(inner={"bpm": 0})
    except Exception as exc:
        error = validation_error_422(exc)

    assert error.status_code == 422
    assert error.message == "inner -> bpm: BPM must be between 60 and 250"
