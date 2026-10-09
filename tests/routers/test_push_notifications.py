"""Admin pushes are addressed to users by external_id, with the device id
saved at /push/register-device as the fallback."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


async def _create_user(db, role=UserRole.user, player_id=None):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Someone",
        role=role,
        onesignal_player_id=player_id,
    )
    db.add(user)
    await db.commit()
    return user


def _auth_headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


def _sent(unreachable=()):
    return AsyncMock(return_value={
        "responses": [{"status_code": 200, "body": {"id": "n1"}}],
        "by_external_id": 1, "by_subscription_id": 0, "unreachable": list(unreachable),
    })


@pytest.mark.asyncio
async def test_send_to_user_without_a_registered_device_uses_the_external_id(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    target = await _create_user(db_session)

    with patch("app.services.onesignal_service.send_to_external_ids", new=_sent()) as send:
        resp = await client.post(
            "/api/v1/push/send/user", headers=_auth_headers(admin),
            json={"user_id": str(target.id), "title": "Hi", "message": "There"},
        )

    assert resp.status_code == 200, resp.text
    args, kwargs = send.call_args
    assert args[0] == [str(target.id)]
    assert kwargs["fallback_subscription_ids"] is None


@pytest.mark.asyncio
async def test_send_to_user_passes_the_registered_device_as_fallback(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    target = await _create_user(db_session, player_id="sub-1")

    with patch("app.services.onesignal_service.send_to_external_ids", new=_sent()) as send:
        await client.post(
            "/api/v1/push/send/user", headers=_auth_headers(admin),
            json={"user_id": str(target.id), "title": "Hi", "message": "There"},
        )

    assert send.call_args.kwargs["fallback_subscription_ids"] == {str(target.id): "sub-1"}


@pytest.mark.asyncio
async def test_send_to_unreachable_user_is_404(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    target = await _create_user(db_session)

    with patch(
        "app.services.onesignal_service.send_to_external_ids",
        new=_sent(unreachable=[str(target.id)]),
    ):
        resp = await client.post(
            "/api/v1/push/send/user", headers=_auth_headers(admin),
            json={"user_id": str(target.id), "title": "Hi", "message": "There"},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_send_to_users_counts_only_the_reachable_ones(client, db_session):
    admin = await _create_user(db_session, UserRole.admin)
    a = await _create_user(db_session, player_id="sub-a")
    b = await _create_user(db_session)

    with patch(
        "app.services.onesignal_service.send_to_external_ids",
        new=_sent(unreachable=[str(b.id)]),
    ) as send:
        resp = await client.post(
            f"/api/v1/push/send/users?user_ids={a.id}&user_ids={b.id}",
            headers=_auth_headers(admin), json={"title": "Hi", "message": "There"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["recipients"] == 1
    assert data["unreachable_user_ids"] == [str(b.id)]
    assert sorted(send.call_args.args[0]) == sorted([str(a.id), str(b.id)])
    assert send.call_args.kwargs["fallback_subscription_ids"] == {str(a.id): "sub-a"}
