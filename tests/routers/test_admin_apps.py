import uuid

import pytest

from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password


async def _create_user(db, role=UserRole.admin):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Admin",
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


PAID_APP = {
    "name": "Toniq",
    "platforms": {
        "macos": "r2://installers/toniq.dmg",
        "windows": "s3://installers/toniq.exe",
        "linux": "https://cdn.example.com/toniq.AppImage",
        "android": "r2://installers/toniq.apk",
        "ios": "https://apps.apple.com/app/id123456789",
    },
    "is_paid": True,
    "price": "15000.00",
    "currency": "NGN",
}


@pytest.mark.asyncio
async def test_admin_creates_and_lists_paid_app(client, db_session):
    admin = await _create_user(db_session)
    resp = await client.post("/api/v1/admin/apps", json=PAID_APP, headers=_auth(admin))
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["name"] == "Toniq"
    assert data["is_paid"] is True
    assert data["price"] == "15000.00"
    assert data["currency"] == "NGN"
    assert data["platforms"] == PAID_APP["platforms"]

    listed = await client.get("/api/v1/admin/apps", headers=_auth(admin))
    assert [a["id"] for a in listed.json()["data"]] == [data["id"]]


@pytest.mark.asyncio
async def test_non_admin_cannot_create_app(client, db_session):
    user = await _create_user(db_session, role=UserRole.user)
    resp = await client.post("/api/v1/admin/apps", json=PAID_APP, headers=_auth(user))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_paid_app_requires_price_and_currency(client, db_session):
    admin = await _create_user(db_session)
    body = {**PAID_APP, "price": None}
    resp = await client.post("/api/v1/admin/apps", json=body, headers=_auth(admin))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_free_app_drops_price(client, db_session):
    admin = await _create_user(db_session)
    body = {**PAID_APP, "is_paid": False}
    resp = await client.post("/api/v1/admin/apps", json=body, headers=_auth(admin))
    assert resp.status_code == 201
    assert resp.json()["data"]["price"] is None
    assert resp.json()["data"]["currency"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("platforms", [
    {"macos": "ftp://x/y"},
    {"macos": "r2://bucket-only"},
    {"windows": "kida.exe"},
    {"freebsd": "https://x/y"},
    {},
])
async def test_bad_platforms_are_rejected(client, db_session, platforms):
    admin = await _create_user(db_session)
    body = {**PAID_APP, "platforms": platforms}
    resp = await client.post("/api/v1/admin/apps", json=body, headers=_auth(admin))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unsupported_currency_is_rejected(client, db_session):
    admin = await _create_user(db_session)
    body = {**PAID_APP, "currency": "EUR"}
    resp = await client.post("/api/v1/admin/apps", json=body, headers=_auth(admin))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_name_conflicts_case_insensitively(client, db_session):
    admin = await _create_user(db_session)
    first = await client.post("/api/v1/admin/apps", json=PAID_APP, headers=_auth(admin))
    assert first.status_code == 201
    dup = await client.post(
        "/api/v1/admin/apps", json={**PAID_APP, "name": "TONIQ"}, headers=_auth(admin)
    )
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_update_merges_platforms(client, db_session):
    admin = await _create_user(db_session)
    created = await client.post("/api/v1/admin/apps", json=PAID_APP, headers=_auth(admin))
    app_id = created.json()["data"]["id"]

    resp = await client.patch(
        f"/api/v1/admin/apps/{app_id}",
        json={"platforms": {"linux": None, "windows": "r2://installers/toniq-2.exe"}},
        headers=_auth(admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["platforms"] == {
        "macos": "r2://installers/toniq.dmg",
        "windows": "r2://installers/toniq-2.exe",
        "android": "r2://installers/toniq.apk",
        "ios": "https://apps.apple.com/app/id123456789",
    }

    # Removing every platform would leave nothing to download.
    empty = await client.patch(
        f"/api/v1/admin/apps/{app_id}",
        json={"platforms": {"macos": None, "windows": None, "android": None, "ios": None}},
        headers=_auth(admin),
    )
    assert empty.status_code == 422


@pytest.mark.asyncio
async def test_update_app_rechecks_merged_state(client, db_session):
    admin = await _create_user(db_session)
    created = await client.post(
        "/api/v1/admin/apps", json={**PAID_APP, "is_paid": False}, headers=_auth(admin)
    )
    app_id = created.json()["data"]["id"]

    # Flipping to paid without a price is refused.
    bad = await client.patch(
        f"/api/v1/admin/apps/{app_id}", json={"is_paid": True}, headers=_auth(admin)
    )
    assert bad.status_code == 422

    good = await client.patch(
        f"/api/v1/admin/apps/{app_id}",
        json={"is_paid": True, "price": "20", "currency": "USD"},
        headers=_auth(admin),
    )
    assert good.status_code == 200, good.text
    assert good.json()["data"]["currency"] == "USD"
    assert good.json()["data"]["price"] == "20.00"


@pytest.mark.asyncio
async def test_delete_app(client, db_session):
    admin = await _create_user(db_session)
    created = await client.post("/api/v1/admin/apps", json=PAID_APP, headers=_auth(admin))
    app_id = created.json()["data"]["id"]
    resp = await client.delete(f"/api/v1/admin/apps/{app_id}", headers=_auth(admin))
    assert resp.status_code == 200
    missing = await client.get(f"/api/v1/admin/apps/{app_id}", headers=_auth(admin))
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_description_is_saved_returned_and_clearable(client, db_session, monkeypatch):
    from unittest.mock import AsyncMock
    from app.services import s3_service
    monkeypatch.setattr(
        s3_service, "generate_r2_presigned_url", AsyncMock(return_value="https://r2/x")
    )
    admin = await _create_user(db_session)
    body = {**PAID_APP, "description": "  A beat-making studio for producers.  "}
    created = await client.post("/api/v1/admin/apps", json=body, headers=_auth(admin))
    assert created.status_code == 201, created.text
    app_id = created.json()["data"]["id"]
    assert created.json()["data"]["description"] == "A beat-making studio for producers."

    fetched = await client.get(f"/api/v1/admin/apps/{app_id}", headers=_auth(admin))
    assert fetched.json()["data"]["description"] == "A beat-making studio for producers."

    public = await client.get("/api/v1/app/apps")
    assert public.json()["data"][0]["description"] == "A beat-making studio for producers."

    # Other updates leave it alone; null clears it.
    renamed = await client.patch(
        f"/api/v1/admin/apps/{app_id}", json={"name": "Toniq 2"}, headers=_auth(admin)
    )
    assert renamed.json()["data"]["description"] == "A beat-making studio for producers."
    cleared = await client.patch(
        f"/api/v1/admin/apps/{app_id}", json={"description": None}, headers=_auth(admin)
    )
    assert cleared.json()["data"]["description"] is None


async def _create_named(client, admin, name):
    resp = await client.post(
        "/api/v1/admin/apps",
        json={"name": name, "platforms": {"windows": f"r2://installers/{name}.exe"}},
        headers=_auth(admin),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


@pytest.mark.asyncio
async def test_new_apps_are_appended_and_can_be_reordered(client, db_session):
    admin = await _create_user(db_session)
    a = await _create_named(client, admin, "Zeta")
    b = await _create_named(client, admin, "Alpha")
    c = await _create_named(client, admin, "Mid")

    # Creation order, not alphabetical.
    listed = await client.get("/api/v1/admin/apps", headers=_auth(admin))
    assert [x["id"] for x in listed.json()["data"]] == [a, b, c]

    resp = await client.put(
        "/api/v1/admin/apps/order", json={"app_ids": [c, a, b]}, headers=_auth(admin)
    )
    assert resp.status_code == 200, resp.text
    assert [x["id"] for x in resp.json()["data"]] == [c, a, b]
    assert [x["position"] for x in resp.json()["data"]] == [0, 1, 2]

    public = await client.get("/api/v1/app/apps")
    assert [x["id"] for x in public.json()["data"]] == [c, a, b]

    # A partial list moves those apps to the front; the rest keep their order.
    partial = await client.put(
        "/api/v1/admin/apps/order", json={"app_ids": [b]}, headers=_auth(admin)
    )
    assert [x["id"] for x in partial.json()["data"]] == [b, c, a]


@pytest.mark.asyncio
async def test_reorder_rejects_unknown_and_duplicate_ids(client, db_session):
    admin = await _create_user(db_session)
    a = await _create_named(client, admin, "One")

    unknown = await client.put(
        "/api/v1/admin/apps/order",
        json={"app_ids": [a, str(uuid.uuid4())]},
        headers=_auth(admin),
    )
    assert unknown.status_code == 404

    dup = await client.put(
        "/api/v1/admin/apps/order", json={"app_ids": [a, a]}, headers=_auth(admin)
    )
    assert dup.status_code == 422


@pytest.mark.asyncio
async def test_non_admin_cannot_reorder(client, db_session):
    user = await _create_user(db_session, role=UserRole.user)
    resp = await client.put(
        "/api/v1/admin/apps/order", json={"app_ids": [str(uuid.uuid4())]}, headers=_auth(user)
    )
    assert resp.status_code == 403
