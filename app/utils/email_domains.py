"""Addresses that can never receive mail.

Registration sends a verification code, so an address on one of these domains
can only ever produce an account that is never verified — a probe, a test
script or a placeholder someone forgot to change. Refusing them up front keeps
those rows out of the users table altogether.

``email-validator`` (behind pydantic's ``EmailStr``) already refuses some
special-use names, but deliberately lets the RFC 2606 example domains through
because test suites use them. This module covers both sets, so the rule does
not depend on which version of that library is installed.
"""

# RFC 2606 / RFC 6761: reserved for documentation, never delegated for mail.
_RESERVED_DOMAINS = frozenset({
    "example.com",
    "example.net",
    "example.org",
})

# Top-level names that do not resolve on the public internet (RFC 2606,
# RFC 6761, RFC 6762, RFC 7686, plus .arpa for infrastructure).
_RESERVED_TLDS = frozenset({
    "example",
    "test",
    "invalid",
    "localhost",
    "local",
    "onion",
    "arpa",
})


def is_undeliverable_domain(email: str) -> bool:
    """True if the address's domain is reserved and can never take mail.

    Subdomains count too: ``mail.example.com`` is as undeliverable as
    ``example.com``.
    """
    _, sep, domain = email.rpartition("@")
    if not sep:
        return False
    domain = domain.strip().rstrip(".").lower()
    if not domain:
        return False
    if domain.rsplit(".", 1)[-1] in _RESERVED_TLDS:
        return True
    return any(domain == d or domain.endswith("." + d) for d in _RESERVED_DOMAINS)
