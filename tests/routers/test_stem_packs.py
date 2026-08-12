import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.loop import Genre
from app.models.purchase import Purchase
from app.models.stem_pack import (
    SongSection,
    Stem,
    StemArrangement,
    StemArrangementItem,
    StemArrangementTrack,
    StemInstrument,
    StemPack,
    StemPackType,
    StemPart,
)
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password

WAV = b"RIFF....WAVEfmt "


async def _create_user(db, role=UserRole.producer):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.com",
        password_hash=await hash_password("pass"),
        full_name="Test",
        role=role,
    )
    db.add(user)
    await db.commit()
    return user


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _create_pack(db, user_id, pack_type=StemPackType.breakdown, title="Test Pack"):
    pack = StemPack(
        id=uuid.uuid4(),
        title=title,
        slug=f"{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}",
        genre=Genre.afrobeat,
        pack_type=pack_type,
        bpm=110,
        key="C",
        tags=[],
        price=Decimal("15.00"),
        created_by=user_id,
    )
    db.add(pack)
    await db.commit()
    return pack


async def _create_part(db, pack_id, section=SongSection.verse, name="Verse", position=0):
    part = StemPart(
        id=uuid.uuid4(), stem_pack_id=pack_id, section=section, name=name, position=position
    )
    db.add(part)
    await db.commit()
    return part


async def _create_stem(
    db, pack_id, instrument=StemInstrument.drums, part_id=None, status="ready"
):
    stem = Stem(
        id=uuid.uuid4(),
        stem_pack_id=pack_id,
        part_id=part_id,
        instrument=instrument,
        label=instrument.value.title(),
        status=status,
        duration=12,
        file_s3_key="stems/encrypted/x.enc" if status == "ready" else None,
        preview_s3_key="stems/previews/x.mp3" if status == "ready" else None,
        aes_key="key" if status == "ready" else None,
        aes_iv="iv" if status == "ready" else None,
    )
    db.add(stem)
    await db.commit()
    return stem


def _upload_patches():
    """Stand in for WAV validation, S3 and the background jobs."""
    return (
        patch(
            "app.services.stem_pack_service.validate_wav_upload",
            new=AsyncMock(return_value=WAV),
        ),
        patch(
            "app.services.stem_pack_service.s3_service.upload_bytes",
            new=AsyncMock(return_value="key"),
        ),
        patch("app.routers.stem_pack_management.process_stem_upload", new=MagicMock()),
    )


# ── pack creation ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pack_defaults_to_long_form(client, db_session):
    producer = await _create_user(db_session)
    resp = await client.post(
        "/api/v1/producer/stem-packs",
        json={"title": "No Type Given", "genre": "Afrobeat", "bpm": 100, "key": "C", "price": "10.00"},
        headers=_headers(producer),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["pack_type"] == "long_form"


@pytest.mark.asyncio
async def test_create_breakdown_pack(client, db_session):
    producer = await _create_user(db_session)
    resp = await client.post(
        "/api/v1/producer/stem-packs",
        json={
            "title": "Amapiano Session",
            "genre": "Amapiano",
            "pack_type": "breakdown",
            "bpm": 112,
            "key": "Am",
            "price": "25.00",
        },
        headers=_headers(producer),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["pack_type"] == "breakdown"


# ── song parts ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_part_names_default_and_increment(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)

    first = await client.post(
        f"/api/v1/producer/stem-packs/{pack.id}/parts",
        json={"section": "verse"},
        headers=_headers(producer),
    )
    second = await client.post(
        f"/api/v1/producer/stem-packs/{pack.id}/parts",
        json={"section": "verse"},
        headers=_headers(producer),
    )

    assert first.json()["data"]["name"] == "Verse"
    assert second.json()["data"]["name"] == "Verse 2"


@pytest.mark.asyncio
async def test_duplicate_part_name_is_rejected(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    await _create_part(db_session, pack.id, name="Chorus")

    resp = await client.post(
        f"/api/v1/producer/stem-packs/{pack.id}/parts",
        json={"section": "chorus", "name": "Chorus"},
        headers=_headers(producer),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_long_form_pack_rejects_parts(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id, pack_type=StemPackType.long_form)

    resp = await client.post(
        f"/api/v1/producer/stem-packs/{pack.id}/parts",
        json={"section": "verse"},
        headers=_headers(producer),
    )
    assert resp.status_code == 422


# ── stem uploads ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_long_form_stem_queues_processing(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id, pack_type=StemPackType.long_form)
    validate, upload, task = _upload_patches()

    with validate, upload, task as mock_task:
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/stems",
            data={"instrument": "bass_guitar"},
            files={"file": ("bass.wav", WAV, "audio/wav")},
            headers=_headers(producer),
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["instrument"] == "bass_guitar"
    # Labelled from the instrument when the producer does not name it.
    assert body["label"] == "Bass Guitar"
    assert body["status"] == "processing"
    mock_task.delay.assert_called_once_with(body["id"])


@pytest.mark.asyncio
async def test_breakdown_stem_requires_a_part(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    validate, upload, task = _upload_patches()

    with validate, upload, task:
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/stems",
            data={"instrument": "drums"},
            files={"file": ("drums.wav", WAV, "audio/wav")},
            headers=_headers(producer),
        )

    assert resp.status_code == 422
    assert "part_id" in resp.json()["message"]


@pytest.mark.asyncio
async def test_long_form_stem_rejects_a_part(client, db_session):
    producer = await _create_user(db_session)
    breakdown = await _create_pack(db_session, producer.id, title="Breakdown")
    part = await _create_part(db_session, breakdown.id)
    long_form = await _create_pack(
        db_session, producer.id, pack_type=StemPackType.long_form, title="Long Form"
    )
    validate, upload, task = _upload_patches()

    with validate, upload, task:
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{long_form.id}/stems",
            data={"instrument": "piano", "part_id": str(part.id)},
            files={"file": ("piano.wav", WAV, "audio/wav")},
            headers=_headers(producer),
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_second_stem_for_same_instrument_conflicts(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    part = await _create_part(db_session, pack.id)
    await _create_stem(db_session, pack.id, StemInstrument.drums, part_id=part.id)
    validate, upload, task = _upload_patches()

    with validate, upload, task:
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/stems",
            data={"instrument": "drums", "part_id": str(part.id)},
            files={"file": ("drums.wav", WAV, "audio/wav")},
            headers=_headers(producer),
        )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_bulk_upload_fills_a_part(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    part = await _create_part(db_session, pack.id, section=SongSection.chorus, name="Chorus")
    validate, upload, task = _upload_patches()

    with validate, upload, task as mock_task:
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/stems/bulk",
            data={
                "instruments": "drums,piano,bass_guitar,vocal,cue,metronome",
                "part_id": str(part.id),
            },
            files=[
                ("files", (f"{i}.wav", WAV, "audio/wav")) for i in range(6)
            ],
            headers=_headers(producer),
        )

    assert resp.status_code == 200
    stems = resp.json()["data"]
    assert [s["instrument"] for s in stems] == [
        "drums", "piano", "bass_guitar", "vocal", "cue", "metronome",
    ]
    assert all(s["part_id"] == str(part.id) for s in stems)
    assert mock_task.delay.call_count == 6


@pytest.mark.asyncio
async def test_bulk_upload_rejects_count_mismatch(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id, pack_type=StemPackType.long_form)
    validate, upload, task = _upload_patches()

    with validate, upload, task:
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/stems/bulk",
            data={"instruments": "drums,piano"},
            files=[("files", ("one.wav", WAV, "audio/wav"))],
            headers=_headers(producer),
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_upload_rejects_unknown_instrument(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id, pack_type=StemPackType.long_form)
    validate, upload, task = _upload_patches()

    with validate, upload, task:
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/stems/bulk",
            data={"instruments": "kazoo"},
            files=[("files", ("one.wav", WAV, "audio/wav"))],
            headers=_headers(producer),
        )

    assert resp.status_code == 422
    assert "kazoo" in resp.json()["message"]


@pytest.mark.asyncio
async def test_producer_cannot_touch_another_producers_pack(client, db_session):
    owner = await _create_user(db_session)
    intruder = await _create_user(db_session)
    pack = await _create_pack(db_session, owner.id)

    resp = await client.post(
        f"/api/v1/producer/stem-packs/{pack.id}/parts",
        json={"section": "verse"},
        headers=_headers(intruder),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_may_manage_another_users_pack(client, db_session):
    producer = await _create_user(db_session)
    admin = await _create_user(db_session, role=UserRole.admin)
    pack = await _create_pack(db_session, producer.id)

    resp = await client.post(
        f"/api/v1/admin/stem-packs/{pack.id}/parts",
        json={"section": "intro"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200


# ── arrangements ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_arrangement_waits_for_stems_to_finish(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    part = await _create_part(db_session, pack.id)
    await _create_stem(db_session, pack.id, part_id=part.id, status="processing")

    with patch("app.routers.stem_pack_management.render_stem_arrangement", new=MagicMock()):
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={"name": "Radio Edit", "items": [{"part_id": str(part.id)}]},
            headers=_headers(producer),
        )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_arrangement_queues_a_render(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    intro = await _create_part(db_session, pack.id, SongSection.intro, "Intro", 0)
    chorus = await _create_part(db_session, pack.id, SongSection.chorus, "Chorus", 1)
    await _create_stem(db_session, pack.id, StemInstrument.drums, part_id=intro.id)
    await _create_stem(db_session, pack.id, StemInstrument.drums, part_id=chorus.id)

    with patch(
        "app.routers.stem_pack_management.render_stem_arrangement", new=MagicMock()
    ) as mock_render:
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={
                "name": "Full Song",
                "items": [
                    {"part_id": str(intro.id)},
                    {"part_id": str(chorus.id), "repeat_count": 2},
                ],
            },
            headers=_headers(producer),
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "rendering"
    # The first arrangement is the pack's default even though it did not ask.
    assert data["is_default"] is True
    assert [i["position"] for i in data["items"]] == [0, 1]
    assert data["items"][1]["repeat_count"] == 2
    mock_render.delay.assert_called_once_with(data["id"])


@pytest.mark.asyncio
async def test_arrangement_rejects_a_part_with_no_stems(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    verse = await _create_part(db_session, pack.id, SongSection.verse, "Verse", 0)
    outro = await _create_part(db_session, pack.id, SongSection.outro, "Outro", 1)
    await _create_stem(db_session, pack.id, part_id=verse.id)

    with patch("app.routers.stem_pack_management.render_stem_arrangement", new=MagicMock()):
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={
                "name": "Full Song",
                "items": [{"part_id": str(verse.id)}, {"part_id": str(outro.id)}],
            },
            headers=_headers(producer),
        )

    # An empty part would contribute no audio and simply vanish from the song.
    assert resp.status_code == 422
    assert "Outro" in resp.json()["message"]


@pytest.mark.asyncio
async def test_arrangement_rejects_a_part_from_another_pack(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id, title="Mine")
    other = await _create_pack(db_session, producer.id, title="Other")
    part = await _create_part(db_session, pack.id)
    foreign = await _create_part(db_session, other.id)
    await _create_stem(db_session, pack.id, part_id=part.id)

    with patch("app.routers.stem_pack_management.render_stem_arrangement", new=MagicMock()):
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={"name": "Mixed", "items": [{"part_id": str(foreign.id)}]},
            headers=_headers(producer),
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_long_form_pack_cannot_be_arranged(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id, pack_type=StemPackType.long_form)

    with patch("app.routers.stem_pack_management.render_stem_arrangement", new=MagicMock()):
        resp = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={"name": "Nope", "items": [{"part_id": str(uuid.uuid4())}]},
            headers=_headers(producer),
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_replacing_a_stem_marks_arrangements_stale(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    part = await _create_part(db_session, pack.id)
    stem = await _create_stem(db_session, pack.id, part_id=part.id)
    arrangement = StemArrangement(
        id=uuid.uuid4(),
        stem_pack_id=pack.id,
        name="Full Song",
        status="ready",
        created_by=producer.id,
    )
    db_session.add(arrangement)
    await db_session.commit()

    validate, upload, task = _upload_patches()
    with validate, upload, task, patch(
        "app.services.stem_pack_service.s3_service.delete_object", new=AsyncMock()
    ):
        resp = await client.patch(
            f"/api/v1/producer/stem-packs/{pack.id}/stems/{stem.id}",
            files={"file": ("new.wav", WAV, "audio/wav")},
            headers=_headers(producer),
        )

    assert resp.status_code == 200
    await db_session.refresh(arrangement)
    assert arrangement.status == "stale"


@pytest.mark.asyncio
async def test_reordering_an_arrangement_rewrites_items_and_re_renders(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    intro = await _create_part(db_session, pack.id, SongSection.intro, "Intro", 0)
    chorus = await _create_part(db_session, pack.id, SongSection.chorus, "Chorus", 1)
    await _create_stem(db_session, pack.id, StemInstrument.drums, part_id=intro.id)
    await _create_stem(db_session, pack.id, StemInstrument.drums, part_id=chorus.id)

    with patch(
        "app.routers.stem_pack_management.render_stem_arrangement", new=MagicMock()
    ) as mock_render:
        created = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={"name": "Full Song", "items": [{"part_id": str(intro.id)}]},
            headers=_headers(producer),
        )
        arrangement_id = created.json()["data"]["id"]

        updated = await client.put(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements/{arrangement_id}",
            json={
                "items": [
                    {"part_id": str(chorus.id), "repeat_count": 2},
                    {"part_id": str(intro.id)},
                ]
            },
            headers=_headers(producer),
        )

    assert updated.status_code == 200
    items = updated.json()["data"]["items"]
    assert [i["part_id"] for i in items] == [str(chorus.id), str(intro.id)]
    assert [i["position"] for i in items] == [0, 1]
    assert updated.json()["data"]["status"] == "rendering"
    assert mock_render.delay.call_count == 2


@pytest.mark.asyncio
async def test_renaming_an_arrangement_does_not_re_render(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    part = await _create_part(db_session, pack.id)
    await _create_stem(db_session, pack.id, part_id=part.id)

    with patch(
        "app.routers.stem_pack_management.render_stem_arrangement", new=MagicMock()
    ) as mock_render:
        created = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={"name": "Full Song", "items": [{"part_id": str(part.id)}]},
            headers=_headers(producer),
        )
        arrangement_id = created.json()["data"]["id"]
        mock_render.reset_mock()

        resp = await client.put(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements/{arrangement_id}",
            json={"name": "Radio Edit"},
            headers=_headers(producer),
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "Radio Edit"
    mock_render.delay.assert_not_called()


@pytest.mark.asyncio
async def test_only_one_arrangement_stays_default(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    part = await _create_part(db_session, pack.id)
    await _create_stem(db_session, pack.id, part_id=part.id)

    with patch("app.routers.stem_pack_management.render_stem_arrangement", new=MagicMock()):
        first = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={"name": "Full Song", "items": [{"part_id": str(part.id)}]},
            headers=_headers(producer),
        )
        await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={
                "name": "Radio Edit",
                "is_default": True,
                "items": [{"part_id": str(part.id)}],
            },
            headers=_headers(producer),
        )
        listed = await client.get(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            headers=_headers(producer),
        )

    defaults = {a["name"]: a["is_default"] for a in listed.json()["data"]}
    assert defaults == {"Full Song": False, "Radio Edit": True}
    assert first.json()["data"]["is_default"] is True


@pytest.mark.asyncio
async def test_deleting_a_part_makes_its_arrangements_stale(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    part = await _create_part(db_session, pack.id)
    await _create_stem(db_session, pack.id, part_id=part.id)

    with patch("app.routers.stem_pack_management.render_stem_arrangement", new=MagicMock()):
        created = await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={"name": "Full Song", "items": [{"part_id": str(part.id)}]},
            headers=_headers(producer),
        )
    arrangement_id = uuid.UUID(created.json()["data"]["id"])
    arrangement = await db_session.get(StemArrangement, arrangement_id)
    arrangement.status = "ready"
    await db_session.commit()

    with patch("app.services.stem_pack_service.s3_service.delete_object", new=AsyncMock()):
        resp = await client.delete(
            f"/api/v1/producer/stem-packs/{pack.id}/parts/{part.id}",
            headers=_headers(producer),
        )

    assert resp.status_code == 200
    await db_session.refresh(arrangement)
    assert arrangement.status == "stale"


# ── downloads ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pack_download_requires_a_purchase(client, db_session):
    producer = await _create_user(db_session)
    buyer = await _create_user(db_session, role=UserRole.user)
    pack = await _create_pack(db_session, producer.id)
    part = await _create_part(db_session, pack.id)
    await _create_stem(db_session, pack.id, part_id=part.id)

    resp = await client.get(
        f"/api/v1/stem-packs/{pack.id}/download", headers=_headers(buyer)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_breakdown_download_tags_each_stem_with_its_part(client, db_session):
    producer = await _create_user(db_session)
    buyer = await _create_user(db_session, role=UserRole.user)
    pack = await _create_pack(db_session, producer.id)
    verse = await _create_part(db_session, pack.id, SongSection.verse, "Verse", 0)
    await _create_stem(db_session, pack.id, StemInstrument.piano, part_id=verse.id)
    await _create_stem(db_session, pack.id, StemInstrument.metronome, part_id=verse.id)
    db_session.add(Purchase(user_id=buyer.id, stem_pack_id=pack.id))
    await db_session.commit()

    with patch(
        "app.routers.stem_packs.s3_service.get_download_url",
        new=AsyncMock(return_value="https://cdn/x"),
    ):
        resp = await client.get(
            f"/api/v1/stem-packs/{pack.id}/download", headers=_headers(buyer)
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["pack_type"] == "breakdown"
    assert {s["instrument"] for s in data["stems"]} == {"piano", "metronome"}
    assert all(s["part_name"] == "Verse" for s in data["stems"])


@pytest.mark.asyncio
async def test_download_skips_stems_still_processing(client, db_session):
    producer = await _create_user(db_session)
    buyer = await _create_user(db_session, role=UserRole.user)
    pack = await _create_pack(db_session, producer.id, pack_type=StemPackType.long_form)
    await _create_stem(db_session, pack.id, StemInstrument.drums, status="processing")
    db_session.add(Purchase(user_id=buyer.id, stem_pack_id=pack.id))
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/stem-packs/{pack.id}/download", headers=_headers(buyer)
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_arrangement_download_returns_rendered_tracks(client, db_session):
    producer = await _create_user(db_session)
    buyer = await _create_user(db_session, role=UserRole.user)
    pack = await _create_pack(db_session, producer.id)
    part = await _create_part(db_session, pack.id)
    await _create_stem(db_session, pack.id, part_id=part.id)

    arrangement = StemArrangement(
        id=uuid.uuid4(),
        stem_pack_id=pack.id,
        name="Full Song",
        status="ready",
        duration=180,
        is_default=True,
        created_by=producer.id,
    )
    db_session.add(arrangement)
    await db_session.flush()
    db_session.add(StemArrangementItem(
        arrangement_id=arrangement.id, part_id=part.id, position=0, repeat_count=1
    ))
    db_session.add(StemArrangementTrack(
        arrangement_id=arrangement.id,
        instrument=StemInstrument.drums,
        file_s3_key="stems/arrangements/encrypted/t.enc",
        aes_key="k",
        aes_iv="i",
        duration=180,
        status="ready",
    ))
    db_session.add(Purchase(user_id=buyer.id, stem_pack_id=pack.id))
    await db_session.commit()

    with patch(
        "app.routers.stem_packs.s3_service.get_download_url",
        new=AsyncMock(return_value="https://cdn/track"),
    ):
        resp = await client.get(
            f"/api/v1/stem-packs/{pack.id}/arrangements/{arrangement.id}/download",
            headers=_headers(buyer),
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["duration"] == 180
    assert data["tracks"][0]["instrument"] == "drums"
    assert data["tracks"][0]["signed_url"] == "https://cdn/track"


@pytest.mark.asyncio
async def test_unrendered_arrangement_cannot_be_downloaded(client, db_session):
    producer = await _create_user(db_session)
    buyer = await _create_user(db_session, role=UserRole.user)
    pack = await _create_pack(db_session, producer.id)
    arrangement = StemArrangement(
        id=uuid.uuid4(),
        stem_pack_id=pack.id,
        name="Pending",
        status="rendering",
        created_by=producer.id,
    )
    db_session.add(arrangement)
    db_session.add(Purchase(user_id=buyer.id, stem_pack_id=pack.id))
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/stem-packs/{pack.id}/arrangements/{arrangement.id}/download",
        headers=_headers(buyer),
    )
    assert resp.status_code == 409


# ── detail view ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_groups_breakdown_stems_under_parts(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    part = await _create_part(db_session, pack.id, SongSection.bridge, "Bridge")
    await _create_stem(db_session, pack.id, StemInstrument.guitar, part_id=part.id)

    with patch(
        "app.routers.stem_packs.s3_service.get_download_url",
        new=AsyncMock(return_value="https://cdn/preview.mp3"),
    ):
        resp = await client.get(
            f"/api/v1/stem-packs/{pack.id}", headers=_headers(producer)
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    # Breakdown stems live under their part, never loose on the pack.
    assert data["stems"] == []
    assert data["parts"][0]["section"] == "bridge"
    assert data["parts"][0]["stems"][0]["instrument"] == "guitar"
    assert data["parts"][0]["stems"][0]["preview_url"] == "https://cdn/preview.mp3"


@pytest.mark.asyncio
async def test_detail_names_each_slot_in_the_running_order(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id)
    intro = await _create_part(db_session, pack.id, SongSection.intro, "Intro", 0)
    chorus = await _create_part(db_session, pack.id, SongSection.chorus, "Chorus", 1)
    await _create_stem(db_session, pack.id, StemInstrument.drums, part_id=intro.id)
    await _create_stem(db_session, pack.id, StemInstrument.drums, part_id=chorus.id)

    with patch("app.routers.stem_pack_management.render_stem_arrangement", new=MagicMock()):
        await client.post(
            f"/api/v1/producer/stem-packs/{pack.id}/arrangements",
            json={
                "name": "Full Song",
                "items": [{"part_id": str(intro.id)}, {"part_id": str(chorus.id)}],
            },
            headers=_headers(producer),
        )

    # A real request starts with an empty identity map, so the parts behind the
    # running order have to be fetched rather than found already in the session.
    # Without that, reaching for them lazily mid-request would fail.
    db_session.expunge_all()

    with patch(
        "app.routers.stem_packs.s3_service.get_download_url",
        new=AsyncMock(return_value="https://cdn/preview.mp3"),
    ):
        detail = await client.get(f"/api/v1/stem-packs/{pack.id}", headers=_headers(producer))
        listing = await client.get("/api/v1/stem-packs", headers=_headers(producer))

    assert detail.status_code == 200
    items = detail.json()["data"]["arrangements"][0]["items"]
    assert [(i["part_name"], i["section"]) for i in items] == [
        ("Intro", "intro"),
        ("Chorus", "chorus"),
    ]
    assert listing.status_code == 200
    listed = next(p for p in listing.json()["data"]["items"] if p["id"] == str(pack.id))
    assert listed["arrangements"][0]["items"][0]["part_name"] == "Intro"


@pytest.mark.asyncio
async def test_upload_status_rolls_up_the_pack(client, db_session):
    producer = await _create_user(db_session)
    pack = await _create_pack(db_session, producer.id, pack_type=StemPackType.long_form)
    await _create_stem(db_session, pack.id, StemInstrument.drums, status="ready")
    await _create_stem(db_session, pack.id, StemInstrument.piano, status="processing")

    resp = await client.get(
        f"/api/v1/producer/stem-packs/{pack.id}/status", headers=_headers(producer)
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "processing"
