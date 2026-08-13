import re
import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from fastapi import UploadFile
from app.models.stem_pack import (
    SongSection,
    Stem,
    StemArrangement,
    StemArrangementItem,
    StemInstrument,
    StemPack,
    StemPackType,
    StemPart,
)
from app.models.purchase import Purchase
from app.models.user import User
from app.schemas.stem_pack import (
    StemCreate,
    StemPackCreate,
    StemPackFilter,
    StemPackUpdate,
    StemPartCreate,
    StemPartUpdate,
)
from app.exceptions import AppError, ConflictError, EntitlementError, NotFoundError
from app.services import s3_service
from app.utils.audio_validator import validate_wav_upload

MAX_STEMS_PER_UPLOAD = len(StemInstrument)
MAX_PARTS_PER_PACK = 40

# How an instrument reads when a producer does not label a stem themselves.
INSTRUMENT_LABELS = {
    StemInstrument.drums: "Drums",
    StemInstrument.piano: "Piano",
    StemInstrument.guitar: "Guitar",
    StemInstrument.bass_guitar: "Bass Guitar",
    StemInstrument.vocal: "Vocal",
    StemInstrument.cue: "Cue",
    StemInstrument.metronome: "Metronome",
    StemInstrument.others: "Others",
}


def _slugify(title: str, uid: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return f"{slug}-{uid[:8]}"


def instrument_label(instrument: StemInstrument) -> str:
    return INSTRUMENT_LABELS.get(instrument, instrument.value.replace("_", " ").title())


def _default_part_name(db_count: int, section: SongSection) -> str:
    """"Verse", then "Verse 2", "Verse 3" — names stay unique within a pack."""
    base = section.value.replace("_", " ").title()
    return base if db_count == 0 else f"{base} {db_count + 1}"


async def create_stem_pack(db: AsyncSession, data: StemPackCreate, created_by: uuid.UUID) -> StemPack:
    pack_id = uuid.uuid4()
    pack = StemPack(
        id=pack_id,
        title=data.title,
        slug=_slugify(data.title, str(pack_id)),
        loop_id=data.loop_id,
        genre=data.genre,
        pack_type=data.pack_type,
        bpm=data.bpm,
        key=data.key,
        tags=data.tags,
        price=data.price,
        description=data.description,
        created_by=created_by,
    )
    db.add(pack)
    await db.commit()
    await db.refresh(pack)
    return pack


async def get_stem_pack(db: AsyncSession, pack_id: uuid.UUID) -> StemPack:
    pack = await db.get(StemPack, pack_id)
    if not pack:
        raise NotFoundError("StemPack not found")
    return pack


async def get_stem_pack_with_stems(db: AsyncSession, pack_id: uuid.UUID) -> StemPack:
    """A pack with everything a detail view needs — stems, parts, arrangements."""
    pack = await db.scalar(
        select(StemPack)
        .options(
            selectinload(StemPack.stems),
            selectinload(StemPack.parts).selectinload(StemPart.stems),
            # The item's part comes along too: clients render the running order
            # by name, and reaching for it lazily inside an async request fails.
            selectinload(StemPack.arrangements)
            .selectinload(StemArrangement.items)
            .selectinload(StemArrangementItem.part),
            selectinload(StemPack.arrangements).selectinload(StemArrangement.tracks),
        )
        .where(StemPack.id == pack_id)
    )
    if not pack:
        raise NotFoundError("StemPack not found")
    return pack


async def update_stem_pack(db: AsyncSession, pack_id: uuid.UUID, data: StemPackUpdate) -> StemPack:
    pack = await get_stem_pack(db, pack_id)
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(pack, field, value)
    await db.commit()
    await db.refresh(pack)
    return pack


async def list_stem_packs(db: AsyncSession, filters: StemPackFilter) -> tuple[list[StemPack], int]:
    q = select(StemPack)
    if filters.created_by:
        q = q.where(StemPack.created_by == filters.created_by)
    if filters.search:
        q = q.where(StemPack.title.ilike(f"%{filters.search}%"))
    if filters.genre:
        q = q.where(StemPack.genre == filters.genre)
    if filters.pack_type:
        q = q.where(StemPack.pack_type == filters.pack_type)
    if filters.tags:
        q = q.where(StemPack.tags.overlap(filters.tags))

    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    q = (
        q.options(
            selectinload(StemPack.stems),
            selectinload(StemPack.parts).selectinload(StemPart.stems),
            # The item's part comes along too: clients render the running order
            # by name, and reaching for it lazily inside an async request fails.
            selectinload(StemPack.arrangements)
            .selectinload(StemArrangement.items)
            .selectinload(StemArrangementItem.part),
            selectinload(StemPack.arrangements).selectinload(StemArrangement.tracks),
        )
        .order_by(StemPack.created_at.desc())
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    return list((await db.scalars(q)).all()), total or 0


# --- Parts (breakdown packs only) ---


def _require_breakdown(pack: StemPack) -> None:
    if pack.pack_type != StemPackType.breakdown:
        raise AppError(
            "Song parts belong to breakdown packs — a long-form pack holds one "
            "continuous stem per instrument",
            status_code=422,
        )


async def create_part(db: AsyncSession, pack_id: uuid.UUID, data: StemPartCreate) -> StemPart:
    pack = await get_stem_pack(db, pack_id)
    _require_breakdown(pack)

    existing = await db.scalar(
        select(func.count()).select_from(StemPart).where(StemPart.stem_pack_id == pack_id)
    ) or 0
    if existing >= MAX_PARTS_PER_PACK:
        raise AppError(f"A pack can have at most {MAX_PARTS_PER_PACK} parts", status_code=422)

    if data.name:
        name = data.name
    else:
        same_section = await db.scalar(
            select(func.count()).select_from(StemPart).where(
                StemPart.stem_pack_id == pack_id, StemPart.section == data.section
            )
        ) or 0
        name = _default_part_name(same_section, data.section)

    clash = await db.scalar(
        select(StemPart.id).where(StemPart.stem_pack_id == pack_id, StemPart.name == name)
    )
    if clash:
        raise ConflictError(f'This pack already has a part named "{name}"')

    part = StemPart(
        stem_pack_id=pack_id,
        section=data.section,
        name=name,
        position=data.position if data.position is not None else existing,
        bars=data.bars,
    )
    db.add(part)
    await db.commit()
    await db.refresh(part)
    return part


async def list_parts(db: AsyncSession, pack_id: uuid.UUID) -> list[StemPart]:
    await get_stem_pack(db, pack_id)
    result = await db.scalars(
        select(StemPart)
        .options(selectinload(StemPart.stems))
        .where(StemPart.stem_pack_id == pack_id)
        .order_by(StemPart.position, StemPart.created_at)
    )
    return list(result.all())


async def get_part(db: AsyncSession, pack_id: uuid.UUID, part_id: uuid.UUID) -> StemPart:
    part = await db.get(StemPart, part_id)
    if not part or part.stem_pack_id != pack_id:
        raise NotFoundError(f"Part {part_id} not found in pack {pack_id}")
    return part


async def update_part(
    db: AsyncSession, pack_id: uuid.UUID, part_id: uuid.UUID, data: StemPartUpdate
) -> StemPart:
    part = await get_part(db, pack_id, part_id)
    fields = data.model_dump(exclude_none=True)
    new_name = fields.get("name")
    if new_name and new_name != part.name:
        clash = await db.scalar(
            select(StemPart.id).where(
                StemPart.stem_pack_id == pack_id,
                StemPart.name == new_name,
                StemPart.id != part_id,
            )
        )
        if clash:
            raise ConflictError(f'This pack already has a part named "{new_name}"')
    for field, value in fields.items():
        setattr(part, field, value)
    await db.commit()
    await db.refresh(part)
    return part


async def delete_part(db: AsyncSession, pack_id: uuid.UUID, part_id: uuid.UUID) -> None:
    """Remove a part, its stems and any arrangement slot that referenced it.

    Arrangements already rendered keep their tracks — the audio is still valid
    for what it was rendered from — but they are marked stale so the producer
    knows to render again.
    """
    part = await get_part(db, pack_id, part_id)
    stems = list((await db.scalars(select(Stem).where(Stem.part_id == part_id))).all())
    keys = _stem_s3_keys(stems)

    affected = list((await db.scalars(
        select(StemArrangement).where(
            StemArrangement.stem_pack_id == pack_id,
            StemArrangement.items.any(StemArrangementItem.part_id == part_id),
        )
    )).all())

    await db.delete(part)  # cascades to its stems and arrangement items
    for arrangement in affected:
        if arrangement.status == "ready":
            arrangement.status = "stale"
    await db.commit()

    for key in keys:
        await s3_service.delete_object(key)


# --- Stems ---


def _stem_s3_keys(stems: list[Stem]) -> list[str]:
    """Every object a set of stems could own, including a stuck raw upload."""
    keys: list[str] = []
    for stem in stems:
        keys += [k for k in (stem.file_s3_key, stem.preview_s3_key) if k]
        keys.append(s3_service.s3_key_for_raw_stem(str(stem.id)))
    return keys


async def _validate_placement(
    db: AsyncSession, pack: StemPack, data: StemCreate
) -> uuid.UUID | None:
    """Check a stem belongs where it says, and return the part id it lands in."""
    if pack.pack_type == StemPackType.breakdown:
        if data.part_id is None:
            raise AppError(
                "part_id is required for a breakdown pack — every stem belongs to a song part",
                status_code=422,
            )
        await get_part(db, pack.id, data.part_id)
        return data.part_id

    if data.part_id is not None:
        raise AppError(
            "part_id is not accepted for a long-form pack — its stems are one "
            "continuous take per instrument",
            status_code=422,
        )
    return None


async def _assert_instrument_free(
    db: AsyncSession, pack_id: uuid.UUID, part_id: uuid.UUID | None, instrument: StemInstrument
) -> None:
    q = select(Stem.id).where(Stem.stem_pack_id == pack_id, Stem.instrument == instrument)
    q = q.where(Stem.part_id == part_id) if part_id else q.where(Stem.part_id.is_(None))
    if await db.scalar(q):
        where = "this part" if part_id else "this pack"
        raise ConflictError(
            f"{instrument_label(instrument)} already has a stem in {where} — "
            "replace its audio instead of adding a second one"
        )


async def add_stems_to_pack(
    db: AsyncSession,
    pack_id: uuid.UUID,
    files: list[UploadFile],
    stems_data: list[StemCreate],
) -> list[Stem]:
    """Upload one or more instrument stems in a single request.

    Files land in S3 raw and the rows start as ``processing``; encryption,
    preview and duration are the background job's work, so a producer sending
    eight instruments at once is not held on the connection while ffmpeg runs.
    """
    if not files:
        raise AppError("At least one stem file is required", status_code=422)
    if len(files) != len(stems_data):
        raise AppError("Number of files must match number of instruments", status_code=422)
    if len(files) > MAX_STEMS_PER_UPLOAD:
        raise AppError(
            f"At most {MAX_STEMS_PER_UPLOAD} stems can be uploaded at once", status_code=422
        )

    pack = await get_stem_pack(db, pack_id)

    seen: set[tuple[uuid.UUID | None, StemInstrument]] = set()
    placements: list[uuid.UUID | None] = []
    for data in stems_data:
        part_id = await _validate_placement(db, pack, data)
        if (part_id, data.instrument) in seen:
            raise AppError(
                f"{instrument_label(data.instrument)} appears twice in this upload",
                status_code=422,
            )
        seen.add((part_id, data.instrument))
        await _assert_instrument_free(db, pack_id, part_id, data.instrument)
        placements.append(part_id)

    created: list[Stem] = []
    for file, data, part_id in zip(files, stems_data, placements):
        wav_bytes = await validate_wav_upload(file)
        stem_id = uuid.uuid4()
        await s3_service.upload_bytes(
            s3_service.s3_key_for_raw_stem(str(stem_id)), wav_bytes, "audio/wav"
        )
        stem = Stem(
            id=stem_id,
            stem_pack_id=pack_id,
            part_id=part_id,
            instrument=data.instrument,
            label=data.label or instrument_label(data.instrument),
            status="processing",
        )
        db.add(stem)
        created.append(stem)

    await db.commit()
    for stem in created:
        await db.refresh(stem)
    return created


async def add_stem_to_pack(
    db: AsyncSession, pack_id: uuid.UUID, file: UploadFile, data: StemCreate
) -> Stem:
    stems = await add_stems_to_pack(db, pack_id, [file], [data])
    return stems[0]


async def get_stem(db: AsyncSession, pack_id: uuid.UUID, stem_id: uuid.UUID) -> Stem:
    stem = await db.get(Stem, stem_id)
    if not stem or stem.stem_pack_id != pack_id:
        raise NotFoundError(f"Stem {stem_id} not found in pack {pack_id}")
    return stem


async def replace_stem_audio(
    db: AsyncSession, pack_id: uuid.UUID, stem_id: uuid.UUID, file: UploadFile
) -> Stem:
    stem = await get_stem(db, pack_id, stem_id)
    wav_bytes = await validate_wav_upload(file)

    old_keys = [k for k in (stem.file_s3_key, stem.preview_s3_key) if k]
    await s3_service.upload_bytes(
        s3_service.s3_key_for_raw_stem(str(stem_id)), wav_bytes, "audio/wav"
    )

    stem.file_s3_key = None
    stem.preview_s3_key = None
    stem.status = "processing"
    # Any arrangement built on this pack now renders from different audio.
    await _mark_arrangements_stale(db, pack_id)
    await db.commit()
    await db.refresh(stem)

    for key in old_keys:
        await s3_service.delete_object(key)
    return stem


async def delete_stem(db: AsyncSession, pack_id: uuid.UUID, stem_id: uuid.UUID) -> None:
    stem = await get_stem(db, pack_id, stem_id)
    keys = _stem_s3_keys([stem])
    await db.delete(stem)
    await _mark_arrangements_stale(db, pack_id)
    await db.commit()
    for key in keys:
        await s3_service.delete_object(key)


async def _mark_arrangements_stale(db: AsyncSession, pack_id: uuid.UUID) -> None:
    arrangements = await db.scalars(
        select(StemArrangement).where(
            StemArrangement.stem_pack_id == pack_id, StemArrangement.status == "ready"
        )
    )
    for arrangement in arrangements.all():
        arrangement.status = "stale"


async def upload_status(db: AsyncSession, pack_id: uuid.UUID) -> dict:
    """Per-stem processing state, plus a single rolled-up verdict for the pack."""
    pack = await get_stem_pack(db, pack_id)
    stems = list((await db.scalars(
        select(Stem).where(Stem.stem_pack_id == pack_id).order_by(Stem.created_at)
    )).all())

    if not stems:
        overall = "empty"
    elif any(s.status == "failed" for s in stems):
        overall = "failed"
    elif all(s.status == "ready" for s in stems):
        overall = "ready"
    else:
        overall = "processing"

    return {
        "id": str(pack.id),
        "pack_type": pack.pack_type.value,
        "status": overall,
        "stems": [
            {
                "id": str(s.id),
                "instrument": s.instrument.value,
                "label": s.label,
                "part_id": str(s.part_id) if s.part_id else None,
                "status": s.status,
            }
            for s in stems
        ],
    }


async def publish(db: AsyncSession, pack_id: uuid.UUID) -> datetime:
    """Mark a pack live so the next new-content digest picks it up.

    Idempotent: publishing again keeps the original timestamp, so a second call
    cannot get the pack into a second digest. Announcement itself is the
    digest's job — nothing is emailed here.
    """
    pack = await get_stem_pack(db, pack_id)
    if pack.published_at is None:
        pack.published_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(pack)
    return pack.published_at


async def delete_stem_pack(db: AsyncSession, pack_id: uuid.UUID) -> None:
    """Delete a pack and everything under it, then its S3 objects."""
    from app.models.stem_pack import StemArrangementTrack

    pack = await get_stem_pack(db, pack_id)
    stems = list((await db.scalars(select(Stem).where(Stem.stem_pack_id == pack_id))).all())
    keys = _stem_s3_keys(stems)

    tracks = await db.scalars(
        select(StemArrangementTrack).join(StemArrangement).where(
            StemArrangement.stem_pack_id == pack_id
        )
    )
    for track in tracks.all():
        keys += [k for k in (track.file_s3_key, track.preview_s3_key) if k]

    await db.delete(pack)  # cascades to stems, parts, arrangements and tracks
    await db.commit()

    for key in keys:
        await s3_service.delete_object(key)


async def check_stem_pack_entitlement(db: AsyncSession, user: User, pack_id: uuid.UUID) -> None:
    purchase = await db.scalar(
        select(Purchase).where(
            Purchase.user_id == user.id,
            Purchase.stem_pack_id == pack_id,
        )
    )
    if not purchase:
        raise EntitlementError("Purchase this stem pack to download")
