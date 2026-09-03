"""Error responses must carry CORS headers, or the browser hides them.

The admin dashboard reported "blocked by CORS policy" for a request that had
really failed with a 500: the catch-all that wrote that 500 sat outside
CORSMiddleware, so the response reached the browser with no
Access-Control-Allow-Origin and was dropped before the client could read the
status or the message.
"""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.middleware.auth_middleware import get_redis

ORIGIN = "http://localhost:3000"


@pytest_asyncio.fixture
async def raw_client(db_session, fake_redis):
    """Renders 500s instead of re-raising them, so the response can be read."""
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_redis] = lambda: fake_redis
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unhandled_error_answers_500_with_cors_headers(raw_client):
    # A bug of our own, raised where any route's would be: nothing handles a
    # bare RuntimeError, so it falls all the way through to the catch-all.
    def _boom():
        raise RuntimeError("boom")

    app.dependency_overrides[get_db] = _boom
    resp = await raw_client.post(
        "/api/v1/auth/login",
        json={"email": "x@test.com", "password": "x"},
        headers={"Origin": ORIGIN},
    )

    assert resp.status_code == 500
    assert resp.json()["message"] == "An unexpected error occurred"
    assert resp.headers["access-control-allow-origin"] == ORIGIN


@pytest.mark.asyncio
async def test_handled_error_answers_with_cors_headers(raw_client):
    """The 404 path shares the chain; a regression there hides errors too."""
    resp = await raw_client.get("/api/v1/loops/not-a-uuid", headers={"Origin": ORIGIN})

    assert resp.status_code in {401, 403, 404, 422}
    assert resp.headers["access-control-allow-origin"] == ORIGIN


@pytest.mark.asyncio
async def test_preflight_for_an_allowed_origin_succeeds(raw_client):
    resp = await raw_client.options(
        "/api/v1/admin/loops/149726d0-35f2-453e-8b99-1a024e31d002",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ORIGIN


@pytest.mark.asyncio
async def test_request_id_is_readable_by_the_browser(raw_client):
    """The header that ties a failure on screen to its log line."""
    resp = await raw_client.get("/health", headers={"Origin": ORIGIN})

    assert "x-request-id" in resp.headers
    exposed = resp.headers["access-control-expose-headers"].lower()
    assert "x-request-id" in exposed
