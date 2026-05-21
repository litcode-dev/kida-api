import asyncio
import httpx
import jwt
from jwt import PyJWKClient
from urllib.parse import urlencode
from app.config import get_settings
from app.exceptions import UnauthorizedError

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
_apple_jwks_client = PyJWKClient(APPLE_JWKS_URL, cache_jwk_set=True, lifespan=3600)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def get_google_auth_url(state: str) -> str:
    settings = get_settings()
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
    async with httpx.AsyncClient() as client:
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
    if resp.status_code != 200:
        raise UnauthorizedError("Failed to exchange Google authorization code")
    return resp.json()


async def get_google_user_info(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise UnauthorizedError("Failed to fetch Google user info")
    return resp.json()


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
