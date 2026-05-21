import pytest
from app.services.email_service import registration_html


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
