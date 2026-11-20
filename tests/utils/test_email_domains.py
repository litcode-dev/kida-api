import pytest

from app.utils.email_domains import is_undeliverable_domain


@pytest.mark.parametrize("email", [
    "kida-desktop-probe-9f31@example.com",
    "someone@EXAMPLE.COM",
    "a@example.net",
    "a@example.org",
    "a@mail.example.com",
    "a@example.com.",
    "a@foo.test",
    "a@foo.invalid",
    "a@localhost",
    "a@printer.local",
    "a@abc.onion",
    "a@host.example",
])
def test_reserved_domains_are_undeliverable(email):
    assert is_undeliverable_domain(email) is True


@pytest.mark.parametrize("email", [
    "user@gmail.com",
    "user@test.com",          # a real registrable .com domain
    "user@myexample.com",     # only the exact domain and its subdomains count
    "user@example.com.ng",
    "user@local.ng",
    "no-at-sign",
])
def test_ordinary_domains_are_allowed(email):
    assert is_undeliverable_domain(email) is False
