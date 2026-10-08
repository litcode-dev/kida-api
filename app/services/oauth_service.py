import asyncio
import time

import httpx
import jwt
import structlog
from jwt import PyJWKClient
from urllib.parse import urlencode
from app.config import get_settings
from app.exceptions import AppError, UnauthorizedError

log = structlog.get_logger()

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
_apple_jwks_client = PyJWKClient(APPLE_JWKS_URL, cache_jwk_set=True, lifespan=3600)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Explicit rather than httpx's 5s default: a slow-but-alive Google is worth
# waiting out, since timing out here costs the user the whole sign-in. Matches
# the timeout the other outbound integrations use.
GOOGLE_TIMEOUT = 15.0

_UNREACHABLE = "Could not reach Google to complete sign-in. Please try again."
_UPSTREAM_FAILED = "Google could not complete the sign-in right now. Please try again."


def _google_error_detail(resp: httpx.Response) -> str:
    """Pull Google's own reason out of an error response, if it sent one.

    Google reports the cause in ``error_description`` (token endpoint) or a
    nested ``error.message`` (userinfo). Surfacing it turns "sign-in failed"
    into something a client can act on, and it is short enough to pass through
    to the caller. Anything unparseable yields "" — the generic message stands.
    """
    try:
        payload = resp.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("error_description") or payload.get("error") or ""
    if isinstance(detail, dict):
        detail = detail.get("message", "")
    return str(detail)[:200] if isinstance(detail, str) else ""


def _with_detail(message: str, detail: str) -> str:
    return f"{message}: {detail}" if detail else message


def _claims_email_verified(value) -> bool | None:
    """Read a provider's email_verified claim. None means it said nothing.

    Apple sends it as the string "true"/"false" in some tokens and a bool in
    others; Google sends a bool. Anything else is treated as unstated rather
    than guessed at.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def reject_unverified_provider_email(provider: str, claim) -> None:
    """Refuse a sign-in whose provider has not verified the email address.

    find_or_create_oauth_user links a provider account to any local account
    with the same address, and marks it verified on the way through. So an
    address the provider never confirmed is enough to take over an existing
    account, which is exactly why both providers publish this claim. Only an
    explicit "false" is refused — an absent claim is left alone, so a provider
    that stops sending it cannot lock every user out.
    """
    if _claims_email_verified(claim) is False:
        log.warning("oauth_email_not_verified", provider=provider)
        raise AppError(
            f"{provider.capitalize()} has not verified this email address. "
            f"Verify it with {provider.capitalize()}, then sign in again.",
            status_code=403,
        )


def get_google_auth_url(state: str) -> str:
    settings = get_settings()
    if not settings.google_client_id:
        raise AppError("Google login is not configured", status_code=503)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_google_code(code: str) -> dict:
    settings = get_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        raise AppError("Google login is not configured", status_code=503)
    try:
        async with httpx.AsyncClient(timeout=GOOGLE_TIMEOUT) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as exc:
        # Timeout, DNS, connection reset: nothing is wrong with the code, so the
        # client should retry rather than restart the whole OAuth dance.
        log.warning("google_token_transport_error", error=str(exc))
        raise AppError(_UNREACHABLE, status_code=503)

    if resp.status_code >= 500:
        log.warning("google_token_upstream_error", status=resp.status_code, body=resp.text[:300])
        raise AppError(_UPSTREAM_FAILED, status_code=502)
    if resp.status_code != 200:
        detail = _google_error_detail(resp)
        log.info("google_token_rejected", status=resp.status_code, detail=detail)
        raise UnauthorizedError(
            _with_detail("Failed to exchange Google authorization code", detail)
        )

    try:
        data = resp.json()
    except ValueError:
        log.warning("google_token_unreadable", body=resp.text[:300])
        raise AppError(_UPSTREAM_FAILED, status_code=502)
    if not isinstance(data, dict) or not data.get("access_token"):
        # A 200 with no token means Google changed the contract on us; the
        # caller would otherwise KeyError its way into a 500.
        log.warning(
            "google_token_missing_access_token",
            keys=sorted(data) if isinstance(data, dict) else type(data).__name__,
        )
        raise AppError(_UPSTREAM_FAILED, status_code=502)
    return data


async def get_google_user_info(access_token: str) -> dict:
    """Fetch the Google profile for an access token.

    Guarantees a dict with non-empty ``sub`` and ``email`` on return, so callers
    can index those two fields without a KeyError.
    """
    try:
        async with httpx.AsyncClient(timeout=GOOGLE_TIMEOUT) as client:
            resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        log.warning("google_userinfo_transport_error", error=str(exc))
        raise AppError(_UNREACHABLE, status_code=503)

    if resp.status_code >= 500:
        log.warning("google_userinfo_upstream_error", status=resp.status_code, body=resp.text[:300])
        raise AppError(_UPSTREAM_FAILED, status_code=502)
    if resp.status_code != 200:
        detail = _google_error_detail(resp)
        log.info("google_userinfo_rejected", status=resp.status_code, detail=detail)
        raise UnauthorizedError(_with_detail("Failed to fetch Google user info", detail))

    try:
        info = resp.json()
    except ValueError:
        log.warning("google_userinfo_unreadable", body=resp.text[:300])
        raise AppError(_UPSTREAM_FAILED, status_code=502)
    if not isinstance(info, dict):
        log.warning("google_userinfo_unexpected_shape", type=type(info).__name__)
        raise AppError(_UPSTREAM_FAILED, status_code=502)

    email = info.get("email")
    if not email:
        # A token minted without the email scope authenticates fine but carries
        # no address, and an account cannot be created without one. The fix is
        # on the client, so say what it has to do differently.
        log.info("google_userinfo_no_email", scopes_hint="email scope missing")
        raise AppError(
            "Google did not provide an email address for this account. "
            "Sign in again and allow access to your email address.",
            status_code=422,
        )

    sub = info.get("sub")
    if not isinstance(email, str) or not isinstance(sub, str) or not sub:
        # Not a scope problem — the payload is not shaped the way the API
        # documents. Passing it on lands it in a query parameter, where the
        # driver rejects it as a raw DataError.
        log.warning(
            "google_userinfo_unexpected_types",
            email_type=type(email).__name__, sub_type=type(sub).__name__,
        )
        raise AppError(_UPSTREAM_FAILED, status_code=502)

    reject_unverified_provider_email("google", info.get("email_verified"))

    # Normalize the fields the callers actually read, so a null or oddly typed
    # name/picture cannot reach the database as a None or a dict.
    profile = dict(info)
    profile["email"] = email.strip()
    profile["sub"] = sub
    name, picture = info.get("name"), info.get("picture")
    profile["name"] = name if isinstance(name, str) else ""
    profile["picture"] = picture if isinstance(picture, str) else None
    return profile


async def verify_apple_identity_token(identity_token: str) -> dict:
    """Verify an Apple identity_token JWT and return its claims."""
    settings = get_settings()
    if not settings.apple_client_id:
        # Without an audience to check against, every token fails as invalid —
        # which reads as the user's fault rather than a missing deployment
        # setting. Matches how the Google routes report the same thing.
        raise AppError("Apple login is not configured", status_code=503)

    def _verify() -> dict:
        signing_key = _apple_jwks_client.get_signing_key_from_jwt(identity_token)
        return jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.apple_client_id,
            issuer="https://appleid.apple.com",
            # sub identifies the account and the router indexes it directly;
            # exp is what stops a leaked token being replayed forever. A token
            # missing either is not one we can act on, and PyJWT raises
            # MissingRequiredClaimError, which the caller already renders as an
            # invalid token rather than a KeyError 500.
            options={"require": ["sub", "exp"]},
        )

    try:
        return await asyncio.to_thread(_verify)
    except jwt.PyJWTError as exc:
        _log_rejected_apple_token(identity_token, exc, settings.apple_client_id)
        raise UnauthorizedError("Invalid Apple identity token")


def _log_rejected_apple_token(identity_token: str, exc: Exception, expected_aud: str) -> None:
    """Record why Apple's token was refused; the client only ever sees a 401.

    The usual culprits read very differently here: an audience that is not our
    bundle ID (Expo Go, a Services ID, a dev bundle), an expired token, or a
    client posting the authorization code, which is not a JWT at all. Only the
    routing claims are logged — never the email or sub.
    """
    try:
        claims = jwt.decode(identity_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        claims = {}
    log.warning(
        "apple_token_rejected",
        reason=type(exc).__name__,
        detail=str(exc)[:200],
        token_aud=claims.get("aud"),
        expected_aud=expected_aud,
        token_iss=claims.get("iss"),
        token_exp=claims.get("exp"),
    )


APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_REVOKE_URL = "https://appleid.apple.com/auth/revoke"
APPLE_TIMEOUT = 15.0

# Apple accepts a client secret valid for up to six months. This one only has
# to outlive the two calls a revocation makes, so it is minted per use.
_APPLE_CLIENT_SECRET_TTL = 300


def apple_revocation_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.apple_client_id
        and settings.apple_team_id
        and settings.apple_siwa_key_id
        and settings.apple_siwa_private_key
    )


def _apple_client_secret() -> str:
    """The ES256 JWT Apple takes in place of a static client secret."""
    settings = get_settings()
    now = int(time.time())
    return jwt.encode(
        {
            "iss": settings.apple_team_id,
            "iat": now,
            "exp": now + _APPLE_CLIENT_SECRET_TTL,
            "aud": "https://appleid.apple.com",
            "sub": settings.apple_client_id,
        },
        settings.apple_siwa_private_key.replace("\\n", "\n"),
        algorithm="ES256",
        headers={"kid": settings.apple_siwa_key_id},
    )


def _json_object(resp: httpx.Response) -> dict | None:
    try:
        payload = resp.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


async def revoke_apple_sign_in(authorization_code: str, expected_sub: str | None) -> bool:
    """Revoke Kiɗa's Sign in with Apple authorization for an account being deleted.

    App Review 5.1.1(v) requires it: deleting an account that signed in with
    Apple must also end the link on Apple's side. Apple only revokes a token,
    and a token only comes from exchanging an authorization code — which is
    single-use and lives five minutes, so it cannot be kept from sign-in. The
    app gets a fresh one by asking the user to sign in with Apple again on the
    way to deletion, and sends it with the delete request.

    Best effort, like the RevenueCat and OneSignal clean-up: this never raises,
    and returns whether Apple confirmed the revocation. Deleting the account
    must not depend on Apple being up.

    ``expected_sub`` is the Apple user id on the account. A code for a different
    Apple ID is not revoked: that would sever someone else's link, not this
    account's.
    """
    if not apple_revocation_configured():
        log.warning("apple_revocation_skipped", reason="not_configured")
        return False
    try:
        client_secret = _apple_client_secret()
    except (ValueError, TypeError, jwt.PyJWTError) as exc:
        # A malformed key is a deployment fault; it must not cost the user the
        # deletion, but it needs to be seen.
        log.error("apple_revocation_skipped", reason="bad_signing_key", error=str(exc)[:200])
        return False

    client_id = get_settings().apple_client_id
    try:
        async with httpx.AsyncClient(timeout=APPLE_TIMEOUT) as client:
            resp = await client.post(
                APPLE_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": authorization_code,
                    "grant_type": "authorization_code",
                },
            )
            tokens = _json_object(resp) if resp.status_code == 200 else None
            if tokens is None:
                log.warning(
                    "apple_code_exchange_failed",
                    status=resp.status_code,
                    body=resp.text[:300],
                )
                return False

            # Straight from Apple over TLS, in answer to a request signed with
            # our key — the signature would tell us nothing more.
            try:
                sub = jwt.decode(
                    tokens.get("id_token") or "", options={"verify_signature": False}
                ).get("sub")
            except jwt.PyJWTError:
                sub = None
            if expected_sub and sub != expected_sub:
                log.warning("apple_revocation_skipped", reason="different_apple_id")
                return False

            if tokens.get("refresh_token"):
                token, hint = tokens["refresh_token"], "refresh_token"
            elif tokens.get("access_token"):
                token, hint = tokens["access_token"], "access_token"
            else:
                log.warning("apple_code_exchange_failed", reason="no_token", keys=sorted(tokens))
                return False

            resp = await client.post(
                APPLE_REVOKE_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "token": token,
                    "token_type_hint": hint,
                },
            )
    except httpx.HTTPError as exc:
        log.warning("apple_revocation_transport_error", error=str(exc)[:200])
        return False

    if resp.status_code != 200:
        log.warning("apple_revocation_failed", status=resp.status_code, body=resp.text[:300])
        return False
    log.info("apple_sign_in_revoked")
    return True
