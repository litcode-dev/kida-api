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
async def test_rejected_apple_token_logs_why(apple_keypair):
    """Every refusal used to surface as the same 401 with nothing in the logs,
    so an audience mismatch could not be told apart from an expired token."""
    from structlog.testing import capture_logs

    token = _apple_token(
        apple_keypair, sub="apple-uid-1", exp=int(time.time()) + 600, aud="host.exp.Exponent"
    )
    with capture_logs() as logs, pytest.raises(UnauthorizedError):
        await oauth_service.verify_apple_identity_token(token)

    [event] = [e for e in logs if e["event"] == "apple_token_rejected"]
    assert event["reason"] == "InvalidAudienceError"
    assert event["token_aud"] == "host.exp.Exponent"
    assert event["expected_aud"] == "app.kida.test"


@pytest.mark.asyncio
async def test_unreadable_apple_token_logs_why(apple_keypair, monkeypatch):
    """Sending the authorization code instead of the identity token is the
    classic client mistake; it is not a JWT at all."""
    import jwt as pyjwt
    from structlog.testing import capture_logs

    def _unreadable(token):
        raise pyjwt.DecodeError("Not enough segments")

    monkeypatch.setattr(oauth_service._apple_jwks_client, "get_signing_key_from_jwt", _unreadable)
    with capture_logs() as logs, pytest.raises(UnauthorizedError):
        await oauth_service.verify_apple_identity_token("c1f2a3b4.0.abcd.not-a-jwt")

    [event] = [e for e in logs if e["event"] == "apple_token_rejected"]
    assert event["reason"] == "DecodeError"
    assert event["token_aud"] is None


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


# ── Revoking Sign in with Apple on account deletion ─────────────────────────

APPLE_SUB = "001234.apple-user.0001"


@pytest.fixture
def apple_revocation(monkeypatch):
    """Configure revocation with a locally generated Sign in with Apple key.

    Returns the key's public half, so a test can check the client secret was
    signed with it.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    get_settings.cache_clear()
    monkeypatch.setenv("APPLE_CLIENT_ID", "com.litcode.kida")
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAM123456")
    monkeypatch.setenv("APPLE_SIWA_KEY_ID", "KEY1234567")
    # Escaped newlines, the way a single-line environment variable carries it.
    monkeypatch.setenv("APPLE_SIWA_PRIVATE_KEY", pem.replace("\n", "\\n"))
    yield private_key.public_key()
    get_settings.cache_clear()


def _apple_id_token(sub=APPLE_SUB) -> str:
    import jwt as pyjwt

    return pyjwt.encode({"sub": sub}, "unused", algorithm="HS256")


def _apple_tokens(**overrides) -> dict:
    return {
        "access_token": "apple-access",
        "refresh_token": "apple-refresh",
        "id_token": _apple_id_token(),
        **overrides,
    }


@pytest.mark.asyncio
async def test_revoke_apple_exchanges_the_code_and_revokes_the_refresh_token(
    apple_revocation,
):
    import jwt as pyjwt

    post = AsyncMock(side_effect=[_mock_response(200, _apple_tokens()), _mock_response(200, {})])
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=_mock_client(post=post)):
        assert await oauth_service.revoke_apple_sign_in("auth-code", APPLE_SUB) is True

    exchange, revoke = post.await_args_list
    assert exchange.args[0] == oauth_service.APPLE_TOKEN_URL
    assert exchange.kwargs["data"]["code"] == "auth-code"
    assert exchange.kwargs["data"]["grant_type"] == "authorization_code"
    assert revoke.args[0] == oauth_service.APPLE_REVOKE_URL
    assert revoke.kwargs["data"]["token"] == "apple-refresh"
    assert revoke.kwargs["data"]["token_type_hint"] == "refresh_token"
    assert revoke.kwargs["data"]["client_id"] == "com.litcode.kida"

    # The client secret is the ES256 JWT Apple specifies, signed with our key.
    secret = revoke.kwargs["data"]["client_secret"]
    assert pyjwt.get_unverified_header(secret)["kid"] == "KEY1234567"
    claims = pyjwt.decode(
        secret, apple_revocation, algorithms=["ES256"], audience="https://appleid.apple.com"
    )
    assert claims["iss"] == "TEAM123456"
    assert claims["sub"] == "com.litcode.kida"


@pytest.mark.asyncio
async def test_revoke_apple_falls_back_to_the_access_token(apple_revocation):
    tokens = _apple_tokens(refresh_token=None)
    post = AsyncMock(side_effect=[_mock_response(200, tokens), _mock_response(200, {})])
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=_mock_client(post=post)):
        assert await oauth_service.revoke_apple_sign_in("auth-code", APPLE_SUB) is True

    revoke = post.await_args_list[1]
    assert revoke.kwargs["data"]["token"] == "apple-access"
    assert revoke.kwargs["data"]["token_type_hint"] == "access_token"


@pytest.mark.asyncio
async def test_revoke_apple_leaves_a_different_apple_id_alone(apple_revocation):
    tokens = _apple_tokens(id_token=_apple_id_token("someone-else"))
    post = AsyncMock(return_value=_mock_response(200, tokens))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=_mock_client(post=post)):
        assert await oauth_service.revoke_apple_sign_in("auth-code", APPLE_SUB) is False

    assert post.await_count == 1, "the exchange only — nothing revoked"


@pytest.mark.asyncio
async def test_revoke_apple_without_a_stored_sub_still_revokes(apple_revocation):
    """Accounts created before the Apple id was recorded have nothing to
    compare against; the code was minted for this deletion, so trust it."""
    post = AsyncMock(side_effect=[_mock_response(200, _apple_tokens()), _mock_response(200, {})])
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=_mock_client(post=post)):
        assert await oauth_service.revoke_apple_sign_in("auth-code", None) is True


@pytest.mark.asyncio
async def test_revoke_apple_rejected_code_returns_false(apple_revocation):
    post = AsyncMock(return_value=_mock_response(400, {"error": "invalid_grant"}))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=_mock_client(post=post)):
        assert await oauth_service.revoke_apple_sign_in("stale-code", APPLE_SUB) is False

    assert post.await_count == 1


@pytest.mark.asyncio
async def test_revoke_apple_failed_revoke_returns_false(apple_revocation):
    post = AsyncMock(side_effect=[_mock_response(200, _apple_tokens()), _mock_response(503)])
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=_mock_client(post=post)):
        assert await oauth_service.revoke_apple_sign_in("auth-code", APPLE_SUB) is False


@pytest.mark.asyncio
async def test_revoke_apple_network_failure_returns_false(apple_revocation):
    post = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=_mock_client(post=post)):
        assert await oauth_service.revoke_apple_sign_in("auth-code", APPLE_SUB) is False


@pytest.mark.asyncio
async def test_revoke_apple_unconfigured_makes_no_calls(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("APPLE_SIWA_PRIVATE_KEY", "")
    try:
        with patch("app.services.oauth_service.httpx.AsyncClient") as client_cls:
            assert await oauth_service.revoke_apple_sign_in("auth-code", APPLE_SUB) is False
        client_cls.assert_not_called()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_revoke_apple_malformed_key_returns_false(apple_revocation, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("APPLE_SIWA_PRIVATE_KEY", "not a key")
    with patch("app.services.oauth_service.httpx.AsyncClient") as client_cls:
        assert await oauth_service.revoke_apple_sign_in("auth-code", APPLE_SUB) is False
    client_cls.assert_not_called()
