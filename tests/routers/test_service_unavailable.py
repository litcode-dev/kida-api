"""Outages answer 503; bugs keep answering 500.

Driven through a real route so the assertions cover the whole chain: the
exception a driver actually raises, the handler registration, and the body the
client receives.
"""
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import httpx
import pytest
import pytest_asyncio
import redis.exceptions
import sqlalchemy.exc
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.middleware.auth_middleware import get_redis


@pytest_asyncio.fixture
async def raw_client(db_session, fake_redis):
    """Like the shared `client`, but renders 500s instead of re-raising them, so
    a test can assert an exception is NOT treated as an outage."""
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _google_userinfo():
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "{}"
    resp.json.return_value = {"sub": "uid-x", "email": "x@test.com", "name": "X"}
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    return client


async def _login_while(raw_client, exc):
    """Sign in with Google while the given exception comes out of the database."""
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=_google_userinfo()), \
         patch("app.services.auth_service.find_or_create_oauth_user", side_effect=exc):
        return await raw_client.post(
            "/api/v1/auth/oauth/google/token", json={"access_token": "t"}
        )


def _dbapi_error(orig: Exception) -> sqlalchemy.exc.DBAPIError:
    return sqlalchemy.exc.DBAPIError("SELECT 1", {}, orig)


@pytest.mark.parametrize("exc", [
    pytest.param(ConnectionRefusedError(111, "Connection refused"), id="postgres-refused"),
    pytest.param(socket.gaierror(-2, "Name or service not known"), id="postgres-dns"),
    pytest.param(asyncpg.exceptions.InternalClientError("connection lost"), id="backend-killed"),
    pytest.param(asyncpg.exceptions.CannotConnectNowError("starting up"), id="postgres-starting"),
    pytest.param(asyncpg.exceptions.AdminShutdownError("shutting down"), id="postgres-shutdown"),
    pytest.param(asyncpg.exceptions.TooManyConnectionsError("too many clients"), id="pool-exhausted"),
    pytest.param(TimeoutError("connect timed out"), id="connect-timeout"),
    pytest.param(sqlalchemy.exc.InterfaceError("SELECT 1", {}, Exception("gone")), id="sa-interface"),
    pytest.param(sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("gone")), id="sa-operational"),
])
@pytest.mark.asyncio
async def test_outage_is_503(raw_client, exc):
    resp = await _login_while(raw_client, exc)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"
    body = resp.json()
    assert body["status"] == "error"
    assert body["message"] == "Service temporarily unavailable. Please try again in a moment."


@pytest.mark.asyncio
async def test_redis_outage_is_503(raw_client, fake_redis):
    async def boom(*args, **kwargs):
        raise redis.exceptions.ConnectionError("Error 111 connecting to redis:6379")

    fake_redis.setex = boom
    with patch("app.services.oauth_service.httpx.AsyncClient", return_value=_google_userinfo()):
        resp = await raw_client.post(
            "/api/v1/auth/oauth/google/token", json={"access_token": "t"}
        )
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"


@pytest.mark.parametrize("exc", [
    pytest.param(_dbapi_error(Exception("statement timeout")), id="statement-timeout"),
    pytest.param(sqlalchemy.exc.ProgrammingError("SELECT 1", {}, Exception("no such column")), id="bad-sql"),
    pytest.param(redis.exceptions.ResponseError("wrong number of arguments"), id="redis-bad-command"),
    pytest.param(FileNotFoundError(2, "No such file"), id="missing-file"),
    pytest.param(ValueError("plain bug"), id="plain-bug"),
])
@pytest.mark.asyncio
async def test_bug_stays_500(raw_client, exc):
    """A bug in our own code must not be dressed up as an outage."""
    resp = await _login_while(raw_client, exc)
    assert resp.status_code == 500
    assert resp.json()["message"] == "An unexpected error occurred"
