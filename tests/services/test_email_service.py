import pytest
from datetime import datetime, timezone

from app.services.email_service import (
    registration_html, app_download_html, app_download_text,
    new_user_admin_html, new_user_admin_text,
    account_deleted_admin_html, account_deleted_admin_text,
    loop_request_admin_html, loop_request_admin_text,
    loop_request_status_html, loop_request_status_subject,
    loop_request_status_text,
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
    assert "https://kida.litcode.com.ng" in html


def test_registration_html_stats():
    html = registration_html("Ada")
    assert "500+" in html
    assert "Loops" in html
    assert "Genres" in html


def test_registration_html_footer():
    html = registration_html("Ada")
    assert "KIDA" in html
    assert "kida.litcode.com.ng" in html


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


_REQUESTED_AT = datetime(2026, 8, 26, 16, 45, tzinfo=timezone.utc)


def _loop_request_fields(**overrides):
    fields = dict(
        request_type="loop",
        artist_name="Tems",
        song_title="Love Me JeJe",
        reference_link="https://example.com/reference",
        notes="Please make it mellow.",
        requester_name="Ada Lovelace",
        requester_email="ada@test.com",
        request_id="0f8fad5b-d9cb-469f-a165-70867728950e",
        requested_at=_REQUESTED_AT,
    )
    fields.update(overrides)
    return fields


def test_loop_request_admin_html_contains_request_details():
    html = loop_request_admin_html(**_loop_request_fields())
    assert "Ada Lovelace" in html
    assert "ada@test.com" in html
    assert "Tems" in html
    assert "Love Me JeJe" in html
    assert "https://example.com/reference" in html
    assert "Please make it mellow." in html
    assert "0f8fad5b-d9cb-469f-a165-70867728950e" in html
    assert "Aug 26, 2026 at 16:45 UTC" in html
    assert "LOOP REQUEST" in html
    assert "Unsubscribe" not in html
    assert "Not a customer email." in html


def test_loop_request_admin_html_labels_a_stems_request():
    html = loop_request_admin_html(**_loop_request_fields(request_type="stems"))
    assert "STEMS REQUEST" in html
    assert "requested stems" in html


def test_loop_request_admin_html_handles_missing_link_and_notes():
    html = loop_request_admin_html(
        **_loop_request_fields(reference_link=None, notes=None)
    )
    assert "none provided" in html
    assert "Notes" not in html


def test_loop_request_admin_html_escapes_user_text():
    html = loop_request_admin_html(
        **_loop_request_fields(notes="<script>alert(1)</script>", song_title="A & B")
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "A &amp; B" in html


def test_loop_request_admin_text_contains_request_details():
    txt = loop_request_admin_text(**_loop_request_fields(request_type="stems"))
    assert 'Ada Lovelace requested stems for "Love Me JeJe".' in txt
    assert "Tems" in txt
    assert "ada@test.com" in txt
    assert "https://example.com/reference" in txt
    assert "Please make it mellow." in txt
    assert "Aug 26, 2026 at 16:45 UTC" in txt


def test_loop_request_admin_text_marks_a_missing_reference():
    txt = loop_request_admin_text(**_loop_request_fields(reference_link=None, notes=None))
    assert "none provided" in txt
    assert "Notes:" not in txt


def _status_fields(**overrides):
    fields = dict(
        full_name="Ada Lovelace",
        status="in_progress",
        request_type="loop",
        artist_name="Tems",
        song_title="Love Me JeJe",
    )
    fields.update(overrides)
    return fields


@pytest.mark.parametrize(
    "status,expected",
    [
        ("in_progress", "We're on"),
        ("fulfilled", "is ready."),
        ("declined", "We can't make"),
    ],
)
def test_loop_request_status_html_leads_with_the_status(status, expected):
    html = loop_request_status_html(**_status_fields(status=status))
    assert expected in html
    assert "Ada Lovelace" in html
    assert "Love Me JeJe" in html
    assert "Tems" in html


def test_loop_request_status_html_is_customer_facing():
    """Unlike the team-inbox notice, this one carries the brand footer."""
    html = loop_request_status_html(**_status_fields())
    assert "Unsubscribe" in html
    assert "Not a customer email." not in html


def test_only_the_fulfilled_notice_links_to_the_app():
    ready = loop_request_status_html(**_status_fields(status="fulfilled"))
    working = loop_request_status_html(**_status_fields(status="in_progress"))
    assert "Open Kida" in ready
    assert "Open Kida" not in working


def test_loop_request_status_html_escapes_the_track():
    html = loop_request_status_html(
        **_status_fields(song_title="<script>alert(1)</script>", artist_name="A & B")
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "A &amp; B" in html


def test_loop_request_status_text_carries_the_track_and_footer():
    txt = loop_request_status_text(**_status_fields(status="declined"))
    assert "Hi Ada Lovelace," in txt
    assert "Love Me JeJe" in txt
    assert "Tems" in txt
    assert "Unsubscribe" in txt


def test_loop_request_status_subject_names_the_type():
    assert loop_request_status_subject("fulfilled", "stems") == (
        "Your stems request is ready"
    )
    assert loop_request_status_subject("fulfilled", "loop") == (
        "Your loop request is ready"
    )
    assert loop_request_status_subject("in_progress", "loop") == (
        "We're working on your loop request"
    )


def test_a_status_nobody_is_emailed_about_raises():
    """A caller that forgets to filter must fail, not send a blank notice."""
    with pytest.raises(KeyError):
        loop_request_status_html(**_status_fields(status="new"))
