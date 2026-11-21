from app.schemas.common import success, error


def test_success_envelope():
    result = success(data={"id": 1}, message="created")
    assert result["status"] == "success"
    assert result["data"]["id"] == 1


def test_error_envelope():
    result = error(message="not found")
    assert result["status"] == "error"
    assert result["message"] == "not found"


def test_success_default_message():
    result = success()
    assert result["message"] == "OK"
    assert result["data"] is None


# Also test UserRegister validation
from app.schemas.user import UserRegister
import pytest
from pydantic import ValidationError


def test_user_register_password_too_short():
    with pytest.raises(ValidationError):
        UserRegister(email="test@test.com", password="short", full_name="Test")


def test_user_register_valid():
    u = UserRegister(email="test@test.com", password="longpassword", full_name="Test User")
    assert u.email == "test@test.com"
