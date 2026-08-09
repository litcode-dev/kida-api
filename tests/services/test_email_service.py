import pytest
from datetime import datetime, timezone

from app.services.email_service import (
    registration_html, app_download_html, app_download_text,
    new_user_admin_html, new_user_admin_text,
    account_deleted_admin_html, account_deleted_admin_text,
)

_SIGNED_UP_AT = datetime(2026, 6, 22, 14, 30, tzinfo=timezone.utc)


def test_registration_html_contains_name():
    html = registration_html("Ada")
    assert "Ada" in html


def test_registration_html_hero_section():
    html = registration_html("Ada")
    assert "YOU'RE IN" in html
    assert "Make your" in html
    assert "sound." in html


def test_registration_html_cta():
    html = registration_html("Ada")
    assert "Browse Loops" in html
    assert "https://litmusic.app" in html


def test_registration_html_stats():
    html = registration_html("Ada")
    assert "500+" in html
    assert "Loops" in html
    assert "Genres" in html
    assert "Subscriptions" in html


def test_registration_html_footer():
    html = registration_html("Ada")
    assert "LM" in html
    assert "LITMUSIC" in html


def test_app_download_html_contains_link_and_os():
    exp = datetime(2026, 6, 22, tzinfo=timezone.utc)
    html = app_download_html("macOS", "https://api.example/api/v1/app/download/tok123", exp)
    assert "https://api.example/api/v1/app/download/tok123" in html
    assert "macOS" in html
    assert "Jun 22, 2026" in html


def test_app_download_text_contains_link_and_os():
    exp = datetime(2026, 6, 22, tzinfo=timezone.utc)
    txt = app_download_text("Windows", "https://api.example/api/v1/app/download/tok123", exp)
    assert "https://api.example/api/v1/app/download/tok123" in txt
    assert "Windows" in txt
    assert "Jun 22, 2026" in txt


def test_new_user_admin_html_contains_signup_details():
    html = new_user_admin_html(
        full_name="Ada Lovelace",
        email="ada@test.com",
        provider="google",
        user_id="0f8fad5b-d9cb-469f-a165-70867728950e",
        signed_up_at=_SIGNED_UP_AT,
    )
    assert "Ada Lovelace" in html
    assert "ada@test.com" in html
    assert "google" in html
    assert "0f8fad5b-d9cb-469f-a165-70867728950e" in html
    assert "Jun 22, 2026 at 14:30 UTC" in html
    assert "NEW SIGNUP" in html


def test_new_user_admin_html_has_no_unsubscribe_footer():
    # Internal notification — must not carry the customer-facing unsubscribe link.
    html = new_user_admin_html(
        full_name="Ada", email="ada@test.com", provider="email",
        user_id="abc", signed_up_at=_SIGNED_UP_AT,
    )
    assert "Unsubscribe" not in html
    assert "Not a customer email." in html


def test_new_user_admin_text_contains_signup_details():
    txt = new_user_admin_text(
        full_name="Ada Lovelace",
        email="ada@test.com",
        provider="email",
        user_id="0f8fad5b-d9cb-469f-a165-70867728950e",
        signed_up_at=_SIGNED_UP_AT,
    )
    assert "Ada Lovelace just created a Kida account." in txt
    assert "ada@test.com" in txt
    assert "email" in txt
    assert "0f8fad5b-d9cb-469f-a165-70867728950e" in txt
    assert "Jun 22, 2026 at 14:30 UTC" in txt


_JOINED_AT = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
_DELETED_AT = datetime(2026, 8, 9, 11, 5, tzinfo=timezone.utc)


def test_account_deleted_admin_html_contains_details():
    html = account_deleted_admin_html(
        full_name="Ada Lovelace",
        email="ada@test.com",
        provider="apple",
        user_id="0f8fad5b-d9cb-469f-a165-70867728950e",
        joined_at=_JOINED_AT,
        deleted_at=_DELETED_AT,
    )
    assert "Ada Lovelace deleted their account." in html
    assert "ada@test.com" in html
    assert "apple" in html
    assert "0f8fad5b-d9cb-469f-a165-70867728950e" in html
    assert "Jan 15, 2026" in html
    assert "Aug 09, 2026 at 11:05 UTC" in html
    assert "ACCOUNT DELETED" in html
    assert "Unsubscribe" not in html


def test_account_deleted_admin_handles_unknown_join_date():
    kwargs = dict(
        full_name="Ada", email="ada@test.com", provider="email",
        user_id="abc", joined_at=None, deleted_at=_DELETED_AT,
    )
    assert "unknown" in account_deleted_admin_html(**kwargs)
    assert "unknown" in account_deleted_admin_text(**kwargs)


def test_account_deleted_admin_text_contains_details():
    txt = account_deleted_admin_text(
        full_name="Ada Lovelace",
        email="ada@test.com",
        provider="google",
        user_id="0f8fad5b-d9cb-469f-a165-70867728950e",
        joined_at=_JOINED_AT,
        deleted_at=_DELETED_AT,
    )
    assert "Ada Lovelace deleted their Kida account." in txt
    assert "ada@test.com" in txt
    assert "google" in txt
    assert "Jan 15, 2026" in txt
    assert "Aug 09, 2026 at 11:05 UTC" in txt
