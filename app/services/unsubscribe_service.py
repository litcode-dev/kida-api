"""One-click unsubscribe links for marketing email.

The footer used to carry a ``mailto:`` that someone had to action by hand. That
is not an unsubscribe mechanism — it is a request queue — and Gmail and Yahoo
now require bulk senders to offer a link that works in one click, with no login
and no confirmation step (RFC 8058).

The link carries the address and an HMAC of it. Anyone can unsubscribe an
address they can already read in the email they received, but nobody can
unsubscribe a stranger by guessing URLs, and no token has to be stored.
"""
import hashlib
import hmac
from urllib.parse import quote

from app.config import get_settings

_TOKEN_BYTES = 16  # 32 hex chars — plenty against guessing, short enough to read


def make_token(email: str) -> str:
    """Stable signature for an address. Never expires — an unsubscribe link in a
    two-year-old email must still work."""
    settings = get_settings()
    digest = hmac.new(
        settings.secret_key.encode(),
        email.strip().lower().encode(),
        hashlib.sha256,
    ).hexdigest()
    return digest[: _TOKEN_BYTES * 2]


def verify_token(email: str, token: str) -> bool:
    return hmac.compare_digest(make_token(email), (token or "").strip())


def unsubscribe_url(email: str) -> str | None:
    """The one-click URL for an address, or None when no base URL is configured.

    Callers fall back to the mailto footer rather than emitting a broken link.
    """
    settings = get_settings()
    base = (settings.api_base_url or "").rstrip("/")
    if not base:
        return None
    return (
        f"{base}/api/v1/newsletter/unsubscribe/one-click"
        f"?email={quote(email)}&token={make_token(email)}"
    )


def list_unsubscribe_headers(email: str) -> dict[str, str]:
    """RFC 8058 headers so the mail client shows its own unsubscribe control.

    ``List-Unsubscribe-Post`` is what makes Gmail render the built-in link and
    POST to it directly, which is the form of one-click the big providers
    actually check for.
    """
    url = unsubscribe_url(email)
    if not url:
        return {}
    return {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
