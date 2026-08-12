"""Arrangements: the running order that turns a breakdown pack into a song.

A breakdown pack is uploaded part by part — intro, verse, chorus — with one
stem per instrument in each. An arrangement names the order those parts play
in, and rendering it stitches them back into one continuous file per
instrument. That rendered set is what a buyer downloads and drops into a
session, so it is produced once by a background job rather than on demand.
"""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.exceptions import AppError, ConflictError, NotFoundError
from app.models.stem_pack import (
    Stem,
    StemArrangement,
    StemArrangementItem,
    StemPackType,
    StemPart,
)
from app.schemas.stem_pack import StemArrangementCreate, StemArrangementUpdate
from app.services import s3_service, stem_pack_service

MAX_ARRANGEMENTS_PER_PACK = 20


async def _load(db: AsyncSession, pack_id: uuid.UUID, arrangement_id: uuid.UUID) -> StemArrangement:
    arrangement = await db.scalar(
        select(StemArrangement)
        .options(
            selectinload(StemArrangement.items).selectinload(StemArrangementItem.part),
            selectinload(StemArrangement.tracks),
        )
        .where(StemArrangement.id == arrangement_id)
    )
    if not arrangement or arrangement.stem_pack_id != pack_id:
        raise NotFoundError(f"Arrangement {arrangement_id} not found in pack {pack_id}")
    return arrangement


async def _validate_parts(
    db: AsyncSession, pack_id: uuid.UUID, part_ids: list[uuid.UUID]
) -> None:
    """Every referenced part must belong to this pack — no borrowing across packs."""
    known = set(await db.scalars(
        select(StemPart.id).where(StemPart.stem_pack_id == pack_id, StemPart.id.in_(part_ids))
    ))
    missing = [str(pid) for pid in dict.fromkeys(part_ids) if pid not in known]
    if missing:
        raise AppError(
            f"Part(s) not in this pack: {', '.join(missing)}", status_code=422
        )


async def _require_ready_stems(db: AsyncSession, part_ids: list[uuid.UUID]) -> None:
    """Refuse to render while the audio underneath the arrangement is not there.

    Two ways it can be missing, both of which produce a quietly wrong song
    rather than an error if they are let through: a part with no stems at all
    contributes nothing and vanishes from the running order, and a stem still
    being processed has no encrypted file for the renderer to read, so its
    instrument drops out of the finished track.
    """
    stems = list((await db.scalars(select(Stem).where(Stem.part_id.in_(part_ids)))).all())

    filled = {s.part_id for s in stems}
    empty = [pid for pid in dict.fromkeys(part_ids) if pid not in filled]
    if empty:
        names = list(await db.scalars(
            select(StemPart.name).where(StemPart.id.in_(empty)).order_by(StemPart.position)
        ))
        raise AppError(
            f"No stems uploaded yet for: {', '.join(names)}", status_code=422
        )

    pending = [s for s in stems if s.status != "ready"]
    if pending:
        raise ConflictError(
            f"{len(pending)} stem(s) in this arrangement are still processing — "
            "wait for the pack to finish uploading, then render"
        )


def _item_rows(items) -> list[StemArrangementItem]:
    """Turn the submitted running order into rows, numbered from the top."""
    return [
        StemArrangementItem(
            part_id=item.part_id, position=position, repeat_count=item.repeat_count
        )
        for position, item in enumerate(items)
    ]


async def _replace_items(db: AsyncSession, arrangement: StemArrangement, items) -> None:
    # Emptying the collection deletes the old rows through delete-orphan; the
    # flush has to land before the new ones go in, or the two sets collide on
    # the unique (arrangement, position) constraint.
    arrangement.items.clear()
    await db.flush()
    arrangement.items.extend(_item_rows(items))


async def _clear_other_defaults(
    db: AsyncSession, pack_id: uuid.UUID, keep_id: uuid.UUID | None = None
) -> None:
    others = await db.scalars(
        select(StemArrangement).where(
            StemArrangement.stem_pack_id == pack_id,
            StemArrangement.is_default.is_(True),
        )
    )
    for other in others.all():
        if other.id != keep_id:
            other.is_default = False


async def create_arrangement(
    db: AsyncSession,
    pack_id: uuid.UUID,
    data: StemArrangementCreate,
    created_by: uuid.UUID,
) -> StemArrangement:
    pack = await stem_pack_service.get_stem_pack(db, pack_id)
    if pack.pack_type != StemPackType.breakdown:
        raise AppError(
            "Only breakdown packs are arranged — a long-form pack is already one "
            "continuous take per instrument",
            status_code=422,
        )

    count = await db.scalar(
        select(func.count()).select_from(StemArrangement).where(
            StemArrangement.stem_pack_id == pack_id
        )
    ) or 0
    if count >= MAX_ARRANGEMENTS_PER_PACK:
        raise AppError(
            f"A pack can have at most {MAX_ARRANGEMENTS_PER_PACK} arrangements", status_code=422
        )

    clash = await db.scalar(
        select(StemArrangement.id).where(
            StemArrangement.stem_pack_id == pack_id, StemArrangement.name == data.name
        )
    )
    if clash:
        raise ConflictError(f'This pack already has an arrangement named "{data.name}"')

    part_ids = [item.part_id for item in data.items]
    await _validate_parts(db, pack_id, part_ids)
    await _require_ready_stems(db, part_ids)

    arrangement = StemArrangement(
        stem_pack_id=pack_id,
        name=data.name,
        description=data.description,
        # The first arrangement is the pack's default whether or not it was
        # asked for, so downloading the pack always resolves to something.
        is_default=data.is_default or count == 0,
        status="rendering",
        created_by=created_by,
    )
    # Assigned through the relationship rather than added as loose rows: the
    # session keeps the collection it already has for a new object, so items
    # inserted behind its back would not show up in the response.
    arrangement.items = _item_rows(data.items)
    db.add(arrangement)
    await db.flush()

    if arrangement.is_default:
        await _clear_other_defaults(db, pack_id, keep_id=arrangement.id)

    await db.commit()
    return await _load(db, pack_id, arrangement.id)


async def update_arrangement(
    db: AsyncSession,
    pack_id: uuid.UUID,
    arrangement_id: uuid.UUID,
    data: StemArrangementUpdate,
) -> tuple[StemArrangement, bool]:
    """Edit an arrangement. Returns it with whether a re-render is needed."""
    arrangement = await _load(db, pack_id, arrangement_id)

    if data.name and data.name != arrangement.name:
        clash = await db.scalar(
            select(StemArrangement.id).where(
                StemArrangement.stem_pack_id == pack_id,
                StemArrangement.name == data.name,
                StemArrangement.id != arrangement_id,
            )
        )
        if clash:
            raise ConflictError(f'This pack already has an arrangement named "{data.name}"')
        arrangement.name = data.name

    if data.description is not None:
        arrangement.description = data.description

    if data.is_default is not None:
        arrangement.is_default = data.is_default
        if data.is_default:
            await _clear_other_defaults(db, pack_id, keep_id=arrangement_id)

    should_render = data.items is not None
    if should_render:
        part_ids = [item.part_id for item in data.items]
        await _validate_parts(db, pack_id, part_ids)
        await _require_ready_stems(db, part_ids)
        await _replace_items(db, arrangement, data.items)
        arrangement.status = "rendering"

    await db.commit()
    return await _load(db, pack_id, arrangement_id), should_render


async def rerender(
    db: AsyncSession, pack_id: uuid.UUID, arrangement_id: uuid.UUID
) -> StemArrangement:
    """Queue a fresh render of an unchanged running order.

    Used after the audio underneath an arrangement is replaced, which leaves it
    marked "stale" rather than silently serving tracks built from old stems.
    """
    arrangement = await _load(db, pack_id, arrangement_id)
    if not arrangement.items:
        raise AppError("This arrangement has no parts to render", status_code=422)
    await _require_ready_stems(db, [item.part_id for item in arrangement.items])
    arrangement.status = "rendering"
    await db.commit()
    return await _load(db, pack_id, arrangement_id)


async def list_arrangements(db: AsyncSession, pack_id: uuid.UUID) -> list[StemArrangement]:
    await stem_pack_service.get_stem_pack(db, pack_id)
    result = await db.scalars(
        select(StemArrangement)
        .options(
            selectinload(StemArrangement.items).selectinload(StemArrangementItem.part),
            selectinload(StemArrangement.tracks),
        )
        .where(StemArrangement.stem_pack_id == pack_id)
        .order_by(StemArrangement.created_at)
    )
    return list(result.all())


async def get_arrangement(
    db: AsyncSession, pack_id: uuid.UUID, arrangement_id: uuid.UUID
) -> StemArrangement:
    return await _load(db, pack_id, arrangement_id)


async def default_arrangement(db: AsyncSession, pack_id: uuid.UUID) -> StemArrangement | None:
    """The arrangement a plain pack download resolves to, if there is one."""
    arrangement = await db.scalar(
        select(StemArrangement).where(
            StemArrangement.stem_pack_id == pack_id,
            StemArrangement.is_default.is_(True),
        )
    )
    if arrangement is None:
        arrangement = await db.scalar(
            select(StemArrangement)
            .where(StemArrangement.stem_pack_id == pack_id)
            .order_by(StemArrangement.created_at)
            .limit(1)
        )
    return None if arrangement is None else await _load(db, pack_id, arrangement.id)


async def delete_arrangement(
    db: AsyncSession, pack_id: uuid.UUID, arrangement_id: uuid.UUID
) -> None:
    arrangement = await _load(db, pack_id, arrangement_id)
    keys = [
        k for track in arrangement.tracks
        for k in (track.file_s3_key, track.preview_s3_key) if k
    ]
    was_default = arrangement.is_default
    await db.delete(arrangement)
    await db.flush()

    # Keep exactly one default so a pack download never comes up empty while
    # other arrangements still exist.
    if was_default:
        successor = await db.scalar(
            select(StemArrangement)
            .where(StemArrangement.stem_pack_id == pack_id)
            .order_by(StemArrangement.created_at)
            .limit(1)
        )
        if successor:
            successor.is_default = True

    await db.commit()
    for key in keys:
        await s3_service.delete_object(key)


def timeline(items) -> list[uuid.UUID]:
    """The running order flattened to one part id per play-through.

    A chorus with repeat_count=3 is stored once but plays three times, so the
    renderer wants the expanded list rather than the stored one.
    """
    order: list[uuid.UUID] = []
    for item in sorted(items, key=lambda i: i.position):
        order += [item.part_id] * max(item.repeat_count, 1)
    return order
