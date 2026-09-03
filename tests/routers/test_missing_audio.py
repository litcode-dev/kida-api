"""An object the bucket does not hold must not be served as if it did.

After a bucket migration that moves the database but not the bytes, every row
still reports its loop as ready. Nothing checked, so the preview streamed the
store's error body as audio under a 200, and a download spent a free-tier slot
on a link that 404s.
"""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models.loop import Loop, Genre, TempoFeel
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password

MISSING_KEY_BODY = (
    b'<?xml version="1.0" encoding="UTF-8"?><Error><Code>NoSuchKey</Code>'
    b"<Message>The specified key does not exist.</Message></Error>"
)


async def _user(db, role=UserRole.user):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Test",
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


async def _loop(db, user_id):
    loop = Loop(
        id=uuid.uuid4(),
        title="Test Loop",
        slug=f"test-loop-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        bpm=90,
        duration=4,
        tempo_feel=TempoFeel.mid,
        tags=[],
        price=Decimal("0.00"),
        is_free=True,
        is_paid=False,
        status="ready",
        file_s3_key="loops/encrypted/x.wav.enc",
        preview_s3_key="previews/x_preview.mp3",
        aes_key="k",
        aes_iv="iv",
        created_by=user_id,
    )
    db.add(loop)
    await db.commit()
    return loop


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


def _store_answering(status_code, body=MISSING_KEY_BODY):
    """An httpx transport standing in for the object store."""
    return httpx.MockTransport(lambda request: httpx.Response(status_code, content=body))


@pytest.fixture
def store(monkeypatch):
    def configure(status_code, body=MISSING_KEY_BODY):
        real_client = httpx.AsyncClient

        def build(*args, **kwargs):
            kwargs["transport"] = _store_answering(status_code, body)
            return real_client(*args, **kwargs)

        monkeypatch.setattr("app.utils.object_stream.httpx.AsyncClient", build)

    return configure


@pytest.mark.asyncio
async def test_a_missing_preview_is_not_streamed_as_audio(client, db_session, store):
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)
    store(404)

    with patch("app.services.s3_service.generate_presigned_url", new=AsyncMock(return_value="https://store/x")):
        resp = await client.get(f"/api/v1/loops/{loop.id}/preview", headers=_headers(user))

    assert resp.status_code == 409
    assert resp.json()["data"]["reason"] == "missing_object"
    assert b"NoSuchKey" not in resp.content


@pytest.mark.asyncio
async def test_a_store_refusing_us_is_not_reported_as_a_missing_upload(client, db_session, store):
    """A rejected signature is ours to fix; blaming the upload sends whoever
    reads it looking in the wrong place."""
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)
    store(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")

    with patch("app.services.s3_service.generate_presigned_url", new=AsyncMock(return_value="https://store/x")):
        resp = await client.get(f"/api/v1/loops/{loop.id}/preview", headers=_headers(user))

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_a_preview_that_is_there_still_streams(client, db_session, store):
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)
    store(200, b"ID3-mp3-bytes")

    with patch("app.services.s3_service.generate_presigned_url", new=AsyncMock(return_value="https://store/x")):
        resp = await client.get(f"/api/v1/loops/{loop.id}/preview", headers=_headers(user))

    assert resp.status_code == 200
    assert resp.content == b"ID3-mp3-bytes"
    assert resp.headers["content-type"] == "audio/mpeg"


@pytest.mark.asyncio
async def test_a_missing_file_does_not_cost_a_download(client, db_session):
    """The free-tier slot is a lifetime grant — it must not buy a 404."""
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)

    head = MagicMock(side_effect=_client_error("404"))
    with patch("app.services.s3_service._get_client", return_value=MagicMock(head_object=head)), \
         patch("app.services.free_tier_service.enforce_loop_cap", new=AsyncMock()) as cap:
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 409
    assert resp.json()["data"]["reason"] == "missing_object"
    cap.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_file_that_is_there_still_downloads(client, db_session):
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)

    with patch("app.services.s3_service._get_client", return_value=MagicMock()), \
         patch("app.services.s3_service.get_download_url", new=AsyncMock(return_value="https://store/x")):
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 200


def _client_error(code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "Not Found"}}, "HeadObject")


@pytest.mark.asyncio
async def test_a_store_that_cannot_be_asked_does_not_block_a_download(client, db_session):
    """The check is a safeguard, not a gate. A HEAD the bucket policy denies,
    or a moment's outage, must leave the caller exactly as well off as before
    the check existed — otherwise it becomes an outage of its own."""
    user = await _user(db_session)
    loop = await _loop(db_session, user.id)

    head = MagicMock(side_effect=_client_error("AccessDenied"))
    with patch("app.services.s3_service._get_client", return_value=MagicMock(head_object=head)), \
         patch("app.services.s3_service.get_download_url", new=AsyncMock(return_value="https://store/x")):
        resp = await client.get(f"/api/v1/loops/{loop.id}/download", headers=_headers(user))

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_a_store_error_is_not_read_as_a_missing_object(db_session):
    """Only a 404 means absent. Anything else has to surface as itself."""
    from botocore.exceptions import ClientError
    from app.services import s3_service

    head = MagicMock(side_effect=_client_error("AccessDenied"))
    with patch("app.services.s3_service._get_client", return_value=MagicMock(head_object=head)):
        with pytest.raises(ClientError):
            await s3_service.object_exists("loops/encrypted/x.wav.enc")
