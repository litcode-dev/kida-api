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
    token = create_access_token({"sub": str(producer.id)})
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
    assert resp.status_code == 404


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
    token = create_access_token({"sub": str(producer.id)})
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
    assert resp.status_code == 404
