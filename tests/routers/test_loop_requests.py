import uuid

import pytest
from sqlalchemy import select

from app.models.loop_request import LoopRequest
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


async def _create_user(db):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Requester",
        role=UserRole.user,
    )
    db.add(user)
    await db.commit()
    return user


def _auth_headers(user):
    token = create_access_token(str(user.id), user.role.value)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_user_can_submit_loop_request(client, db_session):
    user = await _create_user(db_session)

    response = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "loop",
            "artist_name": "  Tems  ",
            "song_title": "  Love Me JeJe  ",
            "reference_link": "https://example.com/reference",
            "notes": "  Please make it mellow.  ",
        },
        headers=_auth_headers(user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "success"
    assert body["message"] == "Loop request submitted"
    assert body["data"]["request_type"] == "loop"
    assert body["data"]["artist_name"] == "Tems"
    assert body["data"]["song_title"] == "Love Me JeJe"
    assert body["data"]["reference_link"] == "https://example.com/reference"
    assert body["data"]["notes"] == "Please make it mellow."

    saved = await db_session.scalar(select(LoopRequest))
    assert saved.user_id == user.id
    assert saved.request_type == "loop"
    assert saved.artist_name == "Tems"
    assert saved.song_title == "Love Me JeJe"


@pytest.mark.asyncio
async def test_loop_request_requires_authentication(client):
    response = await client.post(
        "/api/v1/loop-requests",
        json={"request_type": "loop", "artist_name": "Tems", "song_title": "Love Me JeJe"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_loop_request_validates_required_fields_and_link(client, db_session):
    user = await _create_user(db_session)

    missing_title = await client.post(
        "/api/v1/loop-requests",
        json={"request_type": "loop", "artist_name": "Tems"},
        headers=_auth_headers(user),
    )
    invalid_link = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "stems",
            "artist_name": "Tems",
            "song_title": "Love Me JeJe",
            "reference_link": "not-a-url",
        },
        headers=_auth_headers(user),
    )

    assert missing_title.status_code == 422
    assert invalid_link.status_code == 422


@pytest.mark.asyncio
async def test_loop_request_rejects_unknown_request_type(client, db_session):
    user = await _create_user(db_session)

    response = await client.post(
        "/api/v1/loop-requests",
        json={
            "request_type": "drum-kit",
            "artist_name": "Tems",
            "song_title": "Love Me JeJe",
        },
        headers=_auth_headers(user),
    )

    assert response.status_code == 422
