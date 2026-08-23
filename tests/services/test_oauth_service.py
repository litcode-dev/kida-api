import time
from types import SimpleNamespace

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.config import get_settings
from app.services import oauth_service
from app.exceptions import AppError, UnauthorizedError


@pytest.fixture
def google_configured(monkeypatch):
    """Give the Google client credentials real values for this test."""
    get_settings.cache_clear()
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    yield
    get_settings.cache_clear()


def _mock_client(**methods) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    for name, value in methods.items():
        setattr(client, name, value)
    return client


def _mock_response(status_code: int, payload=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "" if payload is None else str(payload)
    if payload is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = payload
    return resp


def test_get_google_auth_url_contains_required_params(google_configured):
    state = "test-state-123"
    url = oauth_service.get_google_auth_url(state)
    assert "accounts.google.com" in url
    assert "state=test-state-123" in url
    assert "response_type=code" in url
    assert "scope=" in url


@pytest.mark.asyncio
async def test_exchange_google_code_success():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "ya29.test-token",
        "token_type": "Bearer",
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=mock_client):
        result = await oauth_service.exchange_google_code("auth-code-xyz")

    assert result["access_token"] == "ya29.test-token"
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert oauth_service.GOOGLE_TOKEN_URL in call_kwargs.args or \
        oauth_service.GOOGLE_TOKEN_URL == call_kwargs.args[0]


@pytest.mark.asyncio
async def test_exchange_google_code_failure_raises():
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.json.return_value = {"error": "invalid_grant"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(UnauthorizedError, match="Failed to exchange Google authorization code"):
            await oauth_service.exchange_google_code("bad-code")


@pytest.mark.asyncio
async def test_get_google_user_info_success():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "sub": "google-uid-12345",
        "email": "user@example.com",
        "name": "Test User",
        "picture": "https://lh3.googleusercontent.com/photo.jpg",
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=mock_client):
        result = await oauth_service.get_google_user_info("ya29.test-token")

    assert result["sub"] == "google-uid-12345"
    assert result["email"] == "user@example.com"
    mock_client.get.assert_called_once()
    call_args = mock_client.get.call_args
    assert "Authorization" in call_args.kwargs.get("headers", {})


@pytest.mark.asyncio
async def test_get_google_user_info_failure_raises():
    mock_response = MagicMock()
    mock_response.status_code = 401

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(UnauthorizedError, match="Failed to fetch Google user info"):
            await oauth_service.get_google_user_info("expired-token")


def test_get_google_auth_url_unconfigured_raises_503(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    try:
        with pytest.raises(AppError, match="Google login is not configured") as exc:
            oauth_service.get_google_auth_url("state")
        assert exc.value.status_code == 503
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_exchange_google_code_unconfigured_raises_503(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
    try:
        with pytest.raises(AppError, match="Google login is not configured") as exc:
            await oauth_service.exchange_google_code("code")
        assert exc.value.status_code == 503
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_exchange_google_code_network_failure_raises_503(google_configured):
    client = _mock_client(post=AsyncMock(side_effect=httpx.ConnectTimeout("timed out")))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        with pytest.raises(AppError, match="Could not reach Google") as exc:
            await oauth_service.exchange_google_code("auth-code")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_exchange_google_code_google_outage_raises_502(google_configured):
    client = _mock_client(post=AsyncMock(return_value=_mock_response(503)))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        with pytest.raises(AppError, match="Google could not complete") as exc:
            await oauth_service.exchange_google_code("auth-code")
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_exchange_google_code_includes_google_reason(google_configured):
    payload = {"error": "invalid_grant", "error_description": "Code was already redeemed."}
    client = _mock_client(post=AsyncMock(return_value=_mock_response(400, payload)))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        with pytest.raises(UnauthorizedError, match="Code was already redeemed") as exc:
            await oauth_service.exchange_google_code("stale-code")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_exchange_google_code_without_access_token_raises_502(google_configured):
    client = _mock_client(post=AsyncMock(return_value=_mock_response(200, {"scope": "openid"})))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        with pytest.raises(AppError, match="Google could not complete") as exc:
            await oauth_service.exchange_google_code("auth-code")
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_get_google_user_info_network_failure_raises_503():
    client = _mock_client(get=AsyncMock(side_effect=httpx.ReadTimeout("timed out")))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        with pytest.raises(AppError, match="Could not reach Google") as exc:
            await oauth_service.get_google_user_info("ya29.token")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_get_google_user_info_google_outage_raises_502():
    client = _mock_client(get=AsyncMock(return_value=_mock_response(500)))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        with pytest.raises(AppError, match="Google could not complete") as exc:
            await oauth_service.get_google_user_info("ya29.token")
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_get_google_user_info_without_email_raises_422():
    """A token minted without the email scope is a client bug, not a server one."""
    payload = {"sub": "google-uid-12345", "name": "Test User"}
    client = _mock_client(get=AsyncMock(return_value=_mock_response(200, payload)))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        with pytest.raises(AppError, match="did not provide an email address") as exc:
            await oauth_service.get_google_user_info("scopeless-token")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_get_google_user_info_without_sub_raises_502():
    payload = {"email": "user@example.com"}
    client = _mock_client(get=AsyncMock(return_value=_mock_response(200, payload)))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        with pytest.raises(AppError, match="Google could not complete") as exc:
            await oauth_service.get_google_user_info("ya29.token")
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_get_google_user_info_unreadable_body_raises_502():
    client = _mock_client(get=AsyncMock(return_value=_mock_response(200)))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        with pytest.raises(AppError, match="Google could not complete") as exc:
            await oauth_service.get_google_user_info("ya29.token")
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_get_google_user_info_unverified_email_is_403():
    """Linking is by email address, so an address Google has not verified is
    enough to take over an existing account."""
    payload = {"sub": "uid", "email": "victim@example.com", "email_verified": False}
    client = _mock_client(get=AsyncMock(return_value=_mock_response(200, payload)))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        with pytest.raises(AppError, match="has not verified this email address") as exc:
            await oauth_service.get_google_user_info("token")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_google_user_info_absent_verified_claim_is_allowed():
    """Only an explicit false is refused — a provider that stops sending the
    claim must not lock everyone out."""
    payload = {"sub": "uid", "email": "user@example.com", "name": "U"}
    client = _mock_client(get=AsyncMock(return_value=_mock_response(200, payload)))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=client):
        result = await oauth_service.get_google_user_info("token")
    assert result["email"] == "user@example.com"


@pytest.mark.parametrize("claim,rejected", [
    (False, True), ("false", True), ("FALSE", True),
    (True, False), ("true", False), (None, False), ("unknown", False),
])
def test_reject_unverified_provider_email(claim, rejected):
    """Apple sends the claim as a string in some tokens and a bool in others."""
    if rejected:
        with pytest.raises(AppError):
            oauth_service.reject_unverified_provider_email("apple", claim)
    else:
        oauth_service.reject_unverified_provider_email("apple", claim)


def _apple_token(private_key, **claims):
    """Mint a token signed the way Apple signs one."""
    import jwt as pyjwt
    from app.config import get_settings

    payload = {
        "iss": "https://appleid.apple.com",
        "aud": get_settings().apple_client_id,
        "iat": int(time.time()),
        **claims,
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256")


@pytest.fixture
def apple_keypair(monkeypatch):
    """Stand in for Apple's signing key, so tokens can be minted locally."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("APPLE_CLIENT_ID", "app.kida.test")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        oauth_service._apple_jwks_client,
        "get_signing_key_from_jwt",
        lambda token: SimpleNamespace(key=private_key.public_key()),
    )
    yield private_key
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_apple_token_without_sub_is_401(apple_keypair):
    """The router indexes claims["sub"], so a token without it used to arrive
    as a KeyError and a 500."""
    token = _apple_token(apple_keypair, exp=int(time.time()) + 600)
    with pytest.raises(UnauthorizedError, match="Invalid Apple identity token"):
        await oauth_service.verify_apple_identity_token(token)


@pytest.mark.asyncio
async def test_apple_token_without_exp_is_401(apple_keypair):
    """PyJWT only checks an expiry that is present, so a token without one
    would have been accepted forever."""
    token = _apple_token(apple_keypair, sub="apple-uid-1")
    with pytest.raises(UnauthorizedError, match="Invalid Apple identity token"):
        await oauth_service.verify_apple_identity_token(token)


@pytest.mark.asyncio
async def test_apple_token_with_required_claims_is_accepted(apple_keypair):
    token = _apple_token(apple_keypair, sub="apple-uid-1", exp=int(time.time()) + 600)
    claims = await oauth_service.verify_apple_identity_token(token)
    assert claims["sub"] == "apple-uid-1"


@pytest.mark.asyncio
async def test_apple_login_unconfigured_is_503(monkeypatch):
    """An unset APPLE_CLIENT_ID made every token fail as invalid."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("APPLE_CLIENT_ID", "")
    try:
        with pytest.raises(AppError, match="Apple login is not configured") as exc:
            await oauth_service.verify_apple_identity_token("any.token.here")
        assert exc.value.status_code == 503
    finally:
        get_settings.cache_clear()
