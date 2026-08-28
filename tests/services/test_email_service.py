import pytest
from datetime import datetime, timezone

from app.services.email_service import (
    registration_html, registration_text, app_download_html, app_download_text,
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


def test_registration_html_lists_what_is_on_offer():
    """The catalogue is loops, drone pads and drum kits — there are no stems yet."""
    html = registration_html("Ada")
    assert "Loops" in html
    assert "Drone Pads" in html
    assert "Drum Kits" in html
    # "stem" alone matches BlinkMacSystemFont in the font stack.
    assert "stem pack" not in html.lower()


def test_registration_html_mentions_the_subscription():
    html = registration_html("Ada")
    assert "Kida Premium" in html
    assert "setlists" in html


def test_registration_text_offers_the_same_catalogue_as_the_html():
    """The plain-text part is what a stripped-down client shows, so it carries
    the same offer rather than a shorter, staler version of it."""
    text = registration_text("Ada")
    for phrase in ("loops", "drone pads", "drum kits", "setlists"):
        assert phrase in text
    assert "stem pack" not in text.lower()


def test_no_customer_email_still_advertises_stem_packs():
    """Five templates carried the old catalogue in their header strip or body.

    Stem packs are not something Kida sells yet, and an email is where somebody
    first learns what is on offer — so the sweep is pinned here rather than
    left to whoever next edits one of these by hand.
    """
    from app.services.email_service import (
        account_deleted_html, broadcast_html, newsletter_subscribe_html,
        newsletter_subscribe_text, purchase_html,
    )

    rendered = [
        registration_html("Ada"),
        registration_text("Ada"),
        account_deleted_html("Ada"),
        purchase_html(
            full_name="Ada", product_title="Lagos Nights",
            product_type="Loop", amount="10.00",
        ),
        newsletter_subscribe_html("a@b.c"),
        newsletter_subscribe_text("a@b.c"),
        broadcast_html("A new drop", "Body copy."),
    ]

    for out in rendered:
        # "stem" alone matches BlinkMacSystemFont in the font stack.
        assert "stem pack" not in out.lower()


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


def test_the_fulfilled_cta_uses_the_configured_app_link(monkeypatch):
    """The button has to follow the setting, not a hardcoded web address."""
    from app.config import get_settings

    monkeypatch.setenv("APP_DEEP_LINK_URL", "https://kida.litcode.com.ng/app/requests")
    get_settings.cache_clear()
    try:
        html = loop_request_status_html(**_status_fields(status="fulfilled"))
        txt = loop_request_status_text(**_status_fields(status="fulfilled"))
    finally:
        get_settings.cache_clear()

    assert 'href="https://kida.litcode.com.ng/app/requests"' in html
    assert "Open Kida" in html
    assert "https://kida.litcode.com.ng/app/requests" in txt


def test_the_app_link_stays_an_https_url(monkeypatch):
    """A kida:// scheme would be a dead button for anyone without the app, and
    some mail clients strip it outright."""
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        html = loop_request_status_html(**_status_fields(status="fulfilled"))
    finally:
        get_settings.cache_clear()

    assert 'href="https://' in html
    assert "kida://" not in html


def test_a_blank_app_link_falls_back_rather_than_rendering_an_empty_button(
    monkeypatch,
):
    from app.config import get_settings

    monkeypatch.setenv("APP_DEEP_LINK_URL", "")
    get_settings.cache_clear()
    try:
        html = loop_request_status_html(**_status_fields(status="fulfilled"))
    finally:
        get_settings.cache_clear()

    assert 'href=""' not in html
    assert 'href="https://kida.litcode.com.ng"' in html


def test_the_admin_answer_replaces_the_canned_closing_line():
    """Both would have the mail answer the same question twice."""
    answer = "We already have this one — search Tems in the app."
    html = loop_request_status_html(
        **_status_fields(status="fulfilled"), admin_response=answer
    )
    txt = loop_request_status_text(
        **_status_fields(status="fulfilled"), admin_response=answer
    )

    assert answer in html
    assert answer in txt
    assert "Open the app and search for it to hear what we made." not in html
    assert "Open the app and search for it to hear what we made." not in txt


def test_without_an_answer_the_canned_copy_stands():
    html = loop_request_status_html(**_status_fields(status="declined"))
    assert "It happens for all sorts of reasons" in html


def test_the_admin_answer_is_escaped_and_keeps_its_line_breaks():
    html = loop_request_status_html(
        **_status_fields(status="declined"),
        admin_response="<b>no</b> & thanks\nask again soon",
    )
    assert "<b>no</b>" not in html
    assert "&lt;b&gt;no&lt;/b&gt; &amp; thanks" in html
    assert "ask again soon" in html
    assert "<br>" in html


def test_the_answer_does_not_displace_the_track_or_the_button():
    html = loop_request_status_html(
        **_status_fields(status="fulfilled"), admin_response="Already in Kida."
    )
    assert "Love Me JeJe" in html
    assert "Tems" in html
    assert "Open Kida" in html


def test_every_email_link_follows_the_site_setting(monkeypatch):
    """Fifteen templates used to carry the domain as a literal. Moving it must
    be a config change, not a hunt through the markup."""
    from app.config import get_settings
    from app.services.email_service import (
        account_unsuspended_html, newsletter_subscribe_text, registration_html,
    )

    monkeypatch.setenv("SITE_URL", "https://example.test")
    get_settings.cache_clear()
    try:
        rendered = [
            registration_html("Ada"),
            account_unsuspended_html("Ada"),
            newsletter_subscribe_text("a@b.c"),
        ]
    finally:
        get_settings.cache_clear()

    for out in rendered:
        assert "https://example.test" in out
        assert "kida.litcode.com.ng" not in out


def test_the_footer_prints_the_host_without_its_scheme(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("SITE_URL", "https://example.test/")
    get_settings.cache_clear()
    try:
        html = registration_html("Ada")
    finally:
        get_settings.cache_clear()

    assert ">example.test</a>" in html
    assert ">https://example.test</a>" not in html
    # A trailing slash in the setting must not become a double slash in a link.
    assert "example.test//" not in html


def test_a_blank_app_link_follows_the_site_rather_than_a_frozen_literal(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("SITE_URL", "https://example.test")
    monkeypatch.setenv("APP_DEEP_LINK_URL", "")
    get_settings.cache_clear()
    try:
        txt = loop_request_status_text(**_status_fields(status="fulfilled"))
    finally:
        get_settings.cache_clear()

    assert "https://example.test" in txt


def _every_rendered_email() -> dict[str, str]:
    """Every template a person can receive, rendered with plausible input.

    Keyed by name so a failure names the template rather than an index.
    """
    from app.services import email_service as mail

    exp = datetime(2026, 6, 22, tzinfo=timezone.utc)
    section = ("loop", "Loops", [type("Item", (), {"title": "Lagos Nights", "subtitle": "Afrobeat"})()])
    rendered = {
        "verification_html": mail.verification_html("Ada", "123456", 15),
        "verification_text": mail.verification_text("Ada", "123456", 15),
        "registration_html": mail.registration_html("Ada"),
        "registration_text": mail.registration_text("Ada"),
        "new_user_admin_html": mail.new_user_admin_html(
            full_name="Ada", email="a@b.c", provider="email",
            user_id="u1", signed_up_at=_SIGNED_UP_AT,
        ),
        "new_user_admin_text": mail.new_user_admin_text(
            full_name="Ada", email="a@b.c", provider="email",
            user_id="u1", signed_up_at=_SIGNED_UP_AT,
        ),
        "account_deleted_html": mail.account_deleted_html("Ada"),
        "account_deleted_text": mail.account_deleted_text("Ada"),
        "account_deleted_admin_html": mail.account_deleted_admin_html(
            full_name="Ada", email="a@b.c", provider="email", user_id="u1",
            joined_at=_SIGNED_UP_AT, deleted_at=_SIGNED_UP_AT, actor="user",
        ),
        "account_deleted_admin_text": mail.account_deleted_admin_text(
            full_name="Ada", email="a@b.c", provider="email", user_id="u1",
            joined_at=_SIGNED_UP_AT, deleted_at=_SIGNED_UP_AT, actor="user",
        ),
        "loop_request_admin_html": mail.loop_request_admin_html(**_loop_request_fields()),
        "loop_request_admin_text": mail.loop_request_admin_text(**_loop_request_fields()),
        "purchase_html": mail.purchase_html(
            full_name="Ada", product_title="Lagos Nights",
            product_type="Loop", amount="10.00",
        ),
        "purchase_text": mail.purchase_text(
            full_name="Ada", product_title="Lagos Nights",
            product_type="Loop", amount="10.00",
        ),
        "newsletter_subscribe_html": mail.newsletter_subscribe_html("a@b.c"),
        "newsletter_subscribe_text": mail.newsletter_subscribe_text("a@b.c"),
        "newsletter_unsubscribe_html": mail.newsletter_unsubscribe_html("a@b.c"),
        "newsletter_unsubscribe_text": mail.newsletter_unsubscribe_text("a@b.c"),
        "app_download_html": mail.app_download_html("macOS", "https://x.test/d", exp),
        "app_download_text": mail.app_download_text("macOS", "https://x.test/d", exp),
        "content_digest_html": mail.content_digest_html([section], 1),
        "content_digest_text": mail.content_digest_text([section], 1),
        "account_suspended_html": mail.account_suspended_html("Ada", "Chargebacks"),
        "account_suspended_text": mail.account_suspended_text("Ada", "Chargebacks"),
        "account_unsuspended_html": mail.account_unsuspended_html("Ada"),
        "account_unsuspended_text": mail.account_unsuspended_text("Ada"),
        "broadcast_html": mail.broadcast_html("A new drop", "Body copy."),
        "broadcast_text": mail.broadcast_text("A new drop", "Body copy."),
        "deletion_request_html": mail.deletion_request_html("https://x.test/c", 60),
        "deletion_request_text": mail.deletion_request_text("https://x.test/c", 60),
    }
    for status in ("in_progress", "fulfilled", "declined"):
        fields = _status_fields(status=status)
        rendered[f"loop_request_status_html:{status}"] = mail.loop_request_status_html(**fields)
        rendered[f"loop_request_status_text:{status}"] = mail.loop_request_status_text(**fields)
        rendered[f"loop_request_status_subject:{status}"] = mail.loop_request_status_subject(
            status, "loop"
        )
    return rendered


def test_no_email_uses_an_em_dash():
    """House style: mail reads in plain sentences, not dashed asides.

    Rendered rather than grepped, so a dash reintroduced through shared copy
    (the status table, the footers) is caught wherever it ends up.
    """
    offenders = [name for name, out in _every_rendered_email().items() if "—" in out]

    assert not offenders, f"em dash in: {', '.join(sorted(offenders))}"
