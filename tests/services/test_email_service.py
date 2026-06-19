import pytest
from datetime import datetime, timezone

from app.services.email_service import registration_html, app_download_html, app_download_text


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
