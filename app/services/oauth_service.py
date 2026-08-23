import asyncio
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

    def _verify() -> dict:
        signing_key = _apple_jwks_client.get_signing_key_from_jwt(identity_token)
        return jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.apple_client_id,
            issuer="https://appleid.apple.com",
        )

    try:
        return await asyncio.to_thread(_verify)
    except jwt.PyJWTError:
        raise UnauthorizedError("Invalid Apple identity token")
