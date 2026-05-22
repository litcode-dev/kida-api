# tests/routers/test_producer_content.py
import pytest
import uuid
from app.models.user import User, UserRole
from app.services.auth_service import hash_password, create_access_token


async def _make_user(db, role=UserRole.user):
    u = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("x"),
        full_name="Test",
        role=role,
    )
    db.add(u)
    await db.commit()
    return u


# ── loops ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_producer_loops_route_exists(client):
    # Without auth, route must return 403 (not 404 — 404 means route is missing)
    resp = await client.post("/api/v1/producer/loops")
    assert resp.status_code != 404, "POST /producer/loops route is missing"


@pytest.mark.asyncio
async def test_admin_loops_removed(client):
    resp = await client.post("/api/v1/admin/loops")
    assert resp.status_code == 404, "POST /admin/loops should be gone"


@pytest.mark.asyncio
async def test_producer_loop_status_route_exists(client):
    fake_id = uuid.uuid4()
    resp = await client.get(f"/api/v1/producer/loops/{fake_id}/status")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_loop_status_removed(client):
    fake_id = uuid.uuid4()
    resp = await client.get(f"/api/v1/admin/loops/{fake_id}/status")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_loop_update_accepts_producer_token(client, db_session):
    producer = await _make_user(db_session, UserRole.producer)
    token = create_access_token(str(producer.id), producer.role.value)
    fake_id = uuid.uuid4()
    resp = await client.put(
        f"/api/v1/producer/loops/{fake_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    # 403 = wrong role; 404 = not found (correct — producer token accepted, loop just doesn't exist)
    assert resp.status_code != 403, "Producer should be allowed to call PUT /producer/loops/{id}"


@pytest.mark.asyncio
async def test_admin_loop_update_removed(client):
    fake_id = uuid.uuid4()
    resp = await client.put(f"/api/v1/admin/loops/{fake_id}")
    assert resp.status_code in {404, 405}


# ── stem-packs ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_producer_stem_packs_route_exists(client):
    resp = await client.post("/api/v1/producer/stem-packs")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_stem_packs_removed(client):
    resp = await client.post("/api/v1/admin/stem-packs")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_producer_stem_pack_update_accepts_producer_token(client, db_session):
    producer = await _make_user(db_session, UserRole.producer)
    token = create_access_token(str(producer.id), producer.role.value)
    fake_id = uuid.uuid4()
    resp = await client.put(
        f"/api/v1/producer/stem-packs/{fake_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "test"},
    )
    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_admin_stem_pack_update_removed(client):
    fake_id = uuid.uuid4()
    resp = await client.put(f"/api/v1/admin/stem-packs/{fake_id}")
    assert resp.status_code in {404, 405}


@pytest.mark.asyncio
async def test_producer_stem_add_route_exists(client):
    fake_id = uuid.uuid4()
    resp = await client.post(f"/api/v1/producer/stem-packs/{fake_id}/stems")
    assert resp.status_code != 404, "POST /producer/stem-packs/{id}/stems route is missing"


@pytest.mark.asyncio
async def test_admin_stem_add_removed(client):
    fake_id = uuid.uuid4()
    resp = await client.post(f"/api/v1/admin/stem-packs/{fake_id}/stems")
    assert resp.status_code == 404, "POST /admin/stem-packs/{id}/stems should be gone"


# ── drones ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_producer_drone_categories_post_exists(client):
    resp = await client.post("/api/v1/producer/drones/categories")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drone_categories_post_removed(client):
    resp = await client.post("/api/v1/admin/drones/categories")
    assert resp.status_code in {404, 405}


@pytest.mark.asyncio
async def test_producer_drone_categories_get_exists(client):
    resp = await client.get("/api/v1/producer/drones/categories")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drone_categories_get_removed(client):
    resp = await client.get("/api/v1/admin/drones/categories")
    assert resp.status_code in {404, 405}


@pytest.mark.asyncio
async def test_producer_drones_post_exists(client):
    resp = await client.post("/api/v1/producer/drones")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drones_post_removed(client):
    resp = await client.post("/api/v1/admin/drones")
    assert resp.status_code in {404, 405}


@pytest.mark.asyncio
async def test_producer_drones_bulk_exists(client):
    resp = await client.post("/api/v1/producer/drones/bulk")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drones_bulk_removed(client):
    resp = await client.post("/api/v1/admin/drones/bulk")
    assert resp.status_code in {404, 405}


@pytest.mark.asyncio
async def test_producer_drone_update_accepts_producer_token(client, db_session):
    producer = await _make_user(db_session, UserRole.producer)
    token = create_access_token(str(producer.id), producer.role.value)
    fake_id = uuid.uuid4()
    resp = await client.put(
        f"/api/v1/producer/drones/{fake_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code != 403, "Producer should be allowed on PUT /producer/drones/{id}"


@pytest.mark.asyncio
async def test_admin_drone_update_removed(client):
    fake_id = uuid.uuid4()
    resp = await client.put(f"/api/v1/admin/drones/{fake_id}")
    assert resp.status_code in {404, 405}


@pytest.mark.asyncio
async def test_producer_drone_bulk_status_exists(client):
    resp = await client.get("/api/v1/producer/drones/bulk/status", params={"ids": str(uuid.uuid4())})
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drone_bulk_status_removed(client):
    resp = await client.get("/api/v1/admin/drones/bulk/status", params={"ids": str(uuid.uuid4())})
    assert resp.status_code in {404, 405}


@pytest.mark.asyncio
async def test_producer_drone_pad_patch_exists(client):
    fake_drone = uuid.uuid4()
    fake_pad = uuid.uuid4()
    resp = await client.patch(f"/api/v1/producer/drones/{fake_drone}/pads/{fake_pad}")
    assert resp.status_code != 404


@pytest.mark.asyncio
async def test_admin_drone_pad_patch_removed(client):
    fake_drone = uuid.uuid4()
    fake_pad = uuid.uuid4()
    resp = await client.patch(f"/api/v1/admin/drones/{fake_drone}/pads/{fake_pad}")
    assert resp.status_code in {404, 405}
