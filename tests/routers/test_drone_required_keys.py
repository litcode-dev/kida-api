"""A drone goes up as a whole set of keys: C#, E, G and A# at minimum.

The rule lives in drone_service.ensure_required_keys, so it holds for admins and
producers alike and for both upload endpoints — which is what closes the
single-pad upload: one pad can never cover four keys.
"""
import io
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import func, select

from app.models.drone_pad import Drone, DronePad
from app.models.user import User, UserRole
from app.services import drone_service
from app.services.auth_service import create_access_token, hash_password


REQUIRED = ["C#", "E", "G", "A#"]


async def _user(db, role):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@test.com",
        password_hash=await hash_password("pass1234"),
        full_name="Uploader",
        role=role,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


def _wav(seconds: float = 0.05, rate: int = 44100) -> bytes:
    """A real (silent) WAV — the validator reads the file, so bytes won't do."""
    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(rate * seconds), dtype="float32"), rate, format="WAV")
    return buf.getvalue()


def _files(count: int):
    return [("files", (f"pad{i}.wav", _wav(), "audio/wav")) for i in range(count)]


def _patch_uploads(module: str):
    """Keep the store and the broker out of it; the rule is what is under test."""
    return (
        patch.object(drone_service.s3_service, "upload_bytes", new=AsyncMock()),
        patch(f"{module}.process_drone_upload", new=MagicMock()),
    )


async def _drone_count(db):
    return await db.scalar(select(func.count()).select_from(Drone))


# --- the rule itself ---------------------------------------------------------


def test_required_keys_are_the_four_agreed_ones():
    assert [key.value for key in drone_service.REQUIRED_KEYS] == REQUIRED


def test_a_full_set_passes_and_extra_keys_are_welcome():
    from app.models.drone_pad import MusicalKey

    drone_service.ensure_required_keys(drone_service.REQUIRED_KEYS)
    drone_service.ensure_required_keys(
        [*drone_service.REQUIRED_KEYS, MusicalKey.C, MusicalKey.F]
    )


def test_a_missing_key_is_named():
    from app.exceptions import AppError
    from app.models.drone_pad import MusicalKey

    with pytest.raises(AppError) as exc:
        drone_service.ensure_required_keys(
            [MusicalKey.C_sharp, MusicalKey.E, MusicalKey.G]
        )

    assert exc.value.status_code == 422
    assert exc.value.data == {"required_keys": REQUIRED, "missing_keys": ["A#"]}
    assert "A#" in exc.value.message


# --- bulk upload -------------------------------------------------------------


@pytest.mark.parametrize(
    "role,prefix,module",
    [
        (UserRole.admin, "/api/v1/admin", "app.routers.admin"),
        (UserRole.producer, "/api/v1/producer", "app.routers.producer"),
    ],
)
@pytest.mark.asyncio
async def test_bulk_upload_with_the_required_keys_is_accepted(
    client, db_session, role, prefix, module
):
    user = await _user(db_session, role)
    s3, task = _patch_uploads(module)

    with s3, task:
        resp = await client.post(
            f"{prefix}/drones/bulk",
            data={"title": "Warm Pad", "keys": ",".join(REQUIRED), "is_free": "true"},
            files=_files(4),
            headers=_headers(user),
        )

    assert resp.status_code == 200, resp.text
    drone_id = resp.json()["data"]["id"]
    pads = await db_session.scalars(
        select(DronePad.key).where(DronePad.drone_id == uuid.UUID(drone_id))
    )
    assert sorted(p.value for p in pads.all()) == sorted(REQUIRED)


@pytest.mark.parametrize(
    "role,prefix,module",
    [
        (UserRole.admin, "/api/v1/admin", "app.routers.admin"),
        (UserRole.producer, "/api/v1/producer", "app.routers.producer"),
    ],
)
@pytest.mark.asyncio
async def test_bulk_upload_missing_a_required_key_is_refused(
    client, db_session, role, prefix, module
):
    user = await _user(db_session, role)
    before = await _drone_count(db_session)
    s3, task = _patch_uploads(module)

    with s3 as upload, task:
        resp = await client.post(
            f"{prefix}/drones/bulk",
            data={"title": "Warm Pad", "keys": "C#,E,G", "is_free": "true"},
            files=_files(3),
            headers=_headers(user),
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["data"] == {"required_keys": REQUIRED, "missing_keys": ["A#"]}
    assert "A#" in body["message"]
    # Refused before anything was stored: no object in the bucket, no row.
    upload.assert_not_awaited()
    assert await _drone_count(db_session) == before


@pytest.mark.asyncio
async def test_extra_keys_on_top_of_the_required_set_are_kept(client, db_session):
    user = await _user(db_session, UserRole.admin)
    keys = [*REQUIRED, "C"]
    s3, task = _patch_uploads("app.routers.admin")

    with s3, task:
        resp = await client.post(
            "/api/v1/admin/drones/bulk",
            data={"title": "Warm Pad", "keys": ",".join(keys), "is_free": "true"},
            files=_files(5),
            headers=_headers(user),
        )

    assert resp.status_code == 200, resp.text
    pads = await db_session.scalars(
        select(DronePad.key).where(DronePad.drone_id == uuid.UUID(resp.json()["data"]["id"]))
    )
    assert sorted(p.value for p in pads.all()) == sorted(keys)


@pytest.mark.asyncio
async def test_a_set_with_none_of_the_required_keys_names_all_four(client, db_session):
    user = await _user(db_session, UserRole.admin)
    s3, task = _patch_uploads("app.routers.admin")

    with s3, task:
        resp = await client.post(
            "/api/v1/admin/drones/bulk",
            data={"title": "Warm Pad", "keys": "C,D,F", "is_free": "true"},
            files=_files(3),
            headers=_headers(user),
        )

    assert resp.status_code == 422
    assert resp.json()["data"]["missing_keys"] == REQUIRED


# --- single-pad upload -------------------------------------------------------


@pytest.mark.parametrize(
    "role,prefix",
    [(UserRole.admin, "/api/v1/admin"), (UserRole.producer, "/api/v1/producer")],
)
@pytest.mark.parametrize("key", ["C", "C#"])
@pytest.mark.asyncio
async def test_single_pad_upload_is_refused(client, db_session, role, prefix, key):
    """Even a required key on its own: a drone is the whole set or nothing."""
    user = await _user(db_session, role)
    before = await _drone_count(db_session)

    with patch.object(drone_service.s3_service, "upload_bytes", new=AsyncMock()) as upload:
        resp = await client.post(
            f"{prefix}/drones",
            data={"title": "Warm Pad", "key": key, "is_free": "true"},
            files={"file": ("pad.wav", _wav(), "audio/wav")},
            headers=_headers(user),
        )

    assert resp.status_code == 422
    assert "bulk" in resp.json()["message"]
    assert resp.json()["data"]["required_keys"] == REQUIRED
    upload.assert_not_awaited()
    assert await _drone_count(db_session) == before


@pytest.mark.asyncio
async def test_a_paid_single_upload_is_told_the_rule_not_the_price(client, db_session):
    """The endpoint is closed, so that is the answer whatever else is missing."""
    user = await _user(db_session, UserRole.admin)

    resp = await client.post(
        "/api/v1/admin/drones",
        data={"title": "Warm Pad", "key": "C#", "is_free": "false"},
        files={"file": ("pad.wav", _wav(), "audio/wav")},
        headers=_headers(user),
    )

    assert resp.status_code == 422
    assert "price" not in resp.json()["message"]
    assert resp.json()["data"]["required_keys"] == REQUIRED


@pytest.mark.asyncio
async def test_the_service_refuses_a_single_pad_even_when_called_directly(db_session):
    """The guard is in the service, so a new caller cannot route around it."""
    from app.exceptions import AppError
    from app.models.drone_pad import MusicalKey
    from app.schemas.drone_pad import DronePadCreate

    user = await _user(db_session, UserRole.admin)
    data = DronePadCreate(
        title="Warm Pad", key=MusicalKey.C_sharp, price=Decimal("0.00"), is_free=True
    )

    with patch.object(drone_service.s3_service, "upload_bytes", new=AsyncMock()) as upload:
        with pytest.raises(AppError) as exc:
            await drone_service.create_drone(db_session, None, data, user.id)

    assert exc.value.status_code == 422
    upload.assert_not_awaited()
