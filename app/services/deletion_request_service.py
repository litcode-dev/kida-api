"""Signed, expiring confirmation links for the public deletion page.

The page has to serve people who cannot sign in — a lost password, a Google
account they no longer control, a phone they no longer have. They give an email
address; if it belongs to an account, we mail that address a link. Possession of
the inbox is the identity check, which is the same standard the account itself
was created under.

Three properties the token has to hold:

* **Unforgeable.** It is an HMAC over the address and an expiry, so nobody can
  construct one for a stranger's account.
* **Expiring.** The expiry is inside the signed material, so it cannot be
  edited. Unlike the newsletter unsubscribe token — which must still work in a
  two-year-old email — this one authorises destruction and is short-lived.
* **Single use.** Enforced with one Redis key, so a link that leaks from a
  forwarded email or a shared screenshot cannot be replayed later.

Nothing is stored when the link is *issued*: no row, no pending-request table,
and so no database of "people who considered leaving" to leak. The only state
is the used-marker written when a link is redeemed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

import structlog
from redis.asyncio import Redis

from app.config import get_settings

log = structlog.get_logger()

# Distinct from every other signature made with SECRET_KEY, so a token minted
# here can never be replayed as an unsubscribe link or an access token.
_TOKEN_PURPOSE = b"kida:account-deletion-request:v1"
_VERSION = "v1"
_SIG_CHARS = 32  # 128 bits of the digest — not forgeable, still short enough to email

_USED_PREFIX = "deletion_confirm_used:"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(email: str, expires_at: int) -> str:
    settings = get_settings()
    message = b"|".join(
        [_TOKEN_PURPOSE, email.strip().lower().encode(), str(expires_at).encode()]
    )
    digest = hmac.new(settings.secret_key.encode(), message, hashlib.sha256).hexdigest()
    return digest[:_SIG_CHARS]


def make_token(email: str, *, now: int | None = None) -> str:
    """Mint a confirmation token for an address.

    Self-contained — ``v1.<expiry>.<email>.<signature>`` — so the confirm
    endpoint needs no lookup table and the frontend forwards one opaque string.
    """
    ttl = get_settings().deletion_request_token_ttl_minutes * 60
    expires_at = int(now if now is not None else time.time()) + ttl
    normalized = email.strip().lower()
    return ".".join(
        [_VERSION, str(expires_at), _b64(normalized.encode()), _sign(normalized, expires_at)]
    )


def parse_token(token: str, *, now: int | None = None) -> str | None:
    """Return the address a token authorises, or None if it is not usable.

    None covers every failure the same way — malformed, wrong signature,
    expired — because the caller must not tell them apart in a response.
    """
    try:
        version, raw_expiry, raw_email, signature = (token or "").strip().split(".")
    except ValueError:
        return None
    if version != _VERSION:
        return None
    try:
        expires_at = int(raw_expiry)
        email = _unb64(raw_email).decode()
    except (ValueError, UnicodeDecodeError):
        return None

    if int(now if now is not None else time.time()) >= expires_at:
        return None
    if not hmac.compare_digest(_sign(email, expires_at), signature):
        return None
    return email


async def claim_token(redis: Redis, token: str) -> bool:
    """Mark a token as spent. False if it has already been used.

    The marker outlives the token itself so a link cannot be replayed inside its
    own window; once the token expires the key is irrelevant and Redis drops it.
    """
    ttl = get_settings().deletion_request_token_ttl_minutes * 60 + 60
    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:32]
    try:
        claimed = await redis.set(f"{_USED_PREFIX}{fingerprint}", "1", ex=ttl, nx=True)
    except TypeError:
        # A Redis stand-in without set(ex=, nx=) support. Fall back to a plain
        # check-then-set; the race it opens is a double confirm of the same
        # account, which is a no-op.
        key = f"{_USED_PREFIX}{fingerprint}"
        if await redis.get(key):
            return False
        await redis.setex(key, ttl, "1")
        return True
    return bool(claimed)


async def start_request(db, email: str) -> bool:
    """Begin the email deletion route for an address.

    Returns whether an account was found — for logging and tests only. The
    endpoint must return the same response either way; the caller is
    responsible for not leaking this.

    The email itself is queued rather than sent inline, so a registered address
    and an unregistered one take the same time to respond. Otherwise the
    endpoint answers the question it is refusing to answer, just in milliseconds
    instead of words.
    """
    from sqlalchemy import func, select

    from app.models.user import User

    normalized = email.strip().lower()
    user = (
        await db.scalars(
            select(User).where(func.lower(User.email) == normalized).limit(1)
        )
    ).first()
    if user is None:
        log.info("deletion_request.no_account")
        return False

    from app.tasks.deletion_tasks import send_deletion_request_email
    send_deletion_request_email.delay(str(user.id))
    log.info("deletion_request.sent", user_id=str(user.id))
    return True


async def confirm_request(db, redis: Redis, token: str) -> bool:
    """Redeem a confirmation link, deleting the account outright.

    Returns whether an account was actually deleted. Like :func:`start_request`
    the endpoint must not pass this on: a valid-but-unknown address and a
    valid-and-deleted one look identical from outside.

    Idempotent. A token for an account that is already gone succeeds without
    doing anything — and a link cannot be redeemed twice in any case.
    """
    from sqlalchemy import func, select

    from app.models.user import User
    from app.services import auth_service

    email = parse_token(token)
    if email is None:
        log.info("deletion_request.invalid_token")
        return False
    if not await claim_token(redis, token):
        log.info("deletion_request.token_already_used")
        return False

    user = (
        await db.scalars(
            select(User).where(func.lower(User.email) == email).limit(1)
        )
    ).first()
    if user is None:
        return False

    await auth_service.delete_user(
        db, user, redis, refresh_token=None, actor="email_request"
    )
    log.info("deletion_request.confirmed", user_id=str(user.id))
    return True


def confirmation_url(token: str) -> str:
    """Where the emailed link points.

    At the frontend, not this API, and the page there asks for a click that
    POSTs back. A bare GET would be actioned by every link-prefetching mail
    client and corporate link scanner between us and the reader — deleting
    accounts nobody confirmed.
    """
    base = (get_settings().frontend_url or "").rstrip("/")
    return f"{base}/delete-account/confirm?token={token}"
