"""The write side of stem packs, shared by the producer and admin routers.

Producers and admins manage packs through the same endpoints under different
prefixes; the only difference is who may touch what. Rather than keep two
copies of seventeen routes in step, the routes are built once here and mounted
twice — with ``enforce_ownership`` on for producers, who may only edit their
own packs, and off for admins, who may edit anyone's.
"""
import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.exceptions import AppError, ForbiddenError
from app.middleware.rate_limit import limiter
from app.models.loop import Genre
from app.models.stem_pack import StemInstrument, StemPackType
from app.schemas.common import success
from app.schemas.stem_pack import (
    StemArrangementCreate,
    StemArrangementUpdate,
    StemCreate,
    StemPackCreate,
    StemPackFilter,
    StemPackResponse,
    StemPackUpdate,
    StemPartCreate,
    StemPartResponse,
    StemPartUpdate,
    StemResponse,
)
from app.services import stem_arrangement_service, stem_pack_service
from app.tasks.render_tasks import render_stem_arrangement
from app.tasks.upload_tasks import process_stem_upload


def _parse_csv(raw: str | None) -> list[str]:
    return [v.strip() for v in raw.split(",") if v.strip()] if raw else []


def _parse_instruments(raw: str) -> list[StemInstrument]:
    values = _parse_csv(raw)
    if not values:
        raise AppError("At least one instrument is required", status_code=422)
    try:
        return [StemInstrument(v) for v in values]
    except ValueError:
        allowed = ", ".join(i.value for i in StemInstrument)
        raise AppError(f"Unknown instrument in '{raw}'. Allowed: {allowed}", status_code=422)


def build_stem_pack_router(actor_dependency, enforce_ownership: bool) -> APIRouter:
    router = APIRouter(prefix="/stem-packs")

    async def owned(db: AsyncSession, pack_id: uuid.UUID, actor):
        pack = await stem_pack_service.get_stem_pack(db, pack_id)
        if enforce_ownership and pack.created_by != actor.id:
            raise ForbiddenError("You do not own this stem pack")
        return pack

    # --- Packs ---

    @router.get("", summary="List stem packs")
    @limiter.limit("60/minute")
    async def list_stem_packs(
        request: Request,
        search: str | None = None,
        genre: Genre | None = None,
        pack_type: StemPackType | None = None,
        tags: str | None = None,
        page: int = 1,
        page_size: int = 20,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        filters = StemPackFilter(
            search=search,
            genre=genre,
            pack_type=pack_type,
            tags=_parse_csv(tags) or None,
            page=page,
            page_size=page_size,
            # Admins see the whole catalogue; producers see their own shelf.
            created_by=actor.id if enforce_ownership else None,
        )
        packs, total = await stem_pack_service.list_stem_packs(db, filters)
        return success({
            "items": [
                StemPackResponse.model_validate(p).model_dump(mode="json") for p in packs
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        })

    @router.post(
        "",
        summary="Create a stem pack",
        description=(
            "Creates the pack itself; stems are uploaded afterwards. `pack_type` decides "
            "the shape of everything that follows: `long_form` takes one continuous stem "
            "per instrument, `breakdown` takes song parts (intro, verse, chorus…) with one "
            "stem per instrument in each, which are later arranged into a full song."
        ),
    )
    @limiter.limit("30/minute")
    async def create_stem_pack(
        request: Request,
        body: StemPackCreate,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        pack = await stem_pack_service.create_stem_pack(db, body, actor.id)
        return success(
            StemPackResponse.model_validate(pack).model_dump(mode="json"), "StemPack created"
        )

    @router.put("/{pack_id}", summary="Update a stem pack")
    @limiter.limit("20/minute")
    async def update_stem_pack(
        request: Request,
        pack_id: uuid.UUID,
        body: StemPackUpdate,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        pack = await stem_pack_service.update_stem_pack(db, pack_id, body)
        return success(
            StemPackResponse.model_validate(pack).model_dump(mode="json"), "StemPack updated"
        )

    @router.delete("/{pack_id}", summary="Delete a stem pack")
    @limiter.limit("20/minute")
    async def delete_stem_pack(
        request: Request,
        pack_id: uuid.UUID,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        await stem_pack_service.delete_stem_pack(db, pack_id)
        return success(message="StemPack deleted")

    @router.get(
        "/{pack_id}/status",
        summary="Upload status",
        description="Per-stem processing state plus one rolled-up verdict for the pack.",
    )
    @limiter.limit("60/minute")
    async def stem_pack_status(
        request: Request,
        pack_id: uuid.UUID,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        return success(await stem_pack_service.upload_status(db, pack_id))

    # --- Song parts (breakdown packs) ---

    @router.post(
        "/{pack_id}/parts",
        summary="Add a song part",
        description=(
            "Adds one section of a breakdown pack. `name` defaults to the section with a "
            "number when it repeats (\"Verse\", \"Verse 2\") and must be unique in the pack, "
            "since arrangements refer to parts by name."
        ),
    )
    @limiter.limit("60/minute")
    async def create_part(
        request: Request,
        pack_id: uuid.UUID,
        body: StemPartCreate,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        part = await stem_pack_service.create_part(db, pack_id, body)
        return success(
            StemPartResponse.model_validate(part).model_dump(mode="json"), "Part created"
        )

    @router.get("/{pack_id}/parts", summary="List song parts")
    @limiter.limit("60/minute")
    async def list_parts(
        request: Request,
        pack_id: uuid.UUID,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        parts = await stem_pack_service.list_parts(db, pack_id)
        return success([
            StemPartResponse.model_validate(p).model_dump(mode="json") for p in parts
        ])

    @router.put("/{pack_id}/parts/{part_id}", summary="Update a song part")
    @limiter.limit("30/minute")
    async def update_part(
        request: Request,
        pack_id: uuid.UUID,
        part_id: uuid.UUID,
        body: StemPartUpdate,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        part = await stem_pack_service.update_part(db, pack_id, part_id, body)
        return success(
            StemPartResponse.model_validate(part).model_dump(mode="json"), "Part updated"
        )

    @router.delete(
        "/{pack_id}/parts/{part_id}",
        summary="Delete a song part",
        description=(
            "Removes the part, its stems and any arrangement slot referring to it. "
            "Arrangements that used the part keep their rendered tracks but are marked "
            "`stale` so they can be rendered again."
        ),
    )
    @limiter.limit("20/minute")
    async def delete_part(
        request: Request,
        pack_id: uuid.UUID,
        part_id: uuid.UUID,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        await stem_pack_service.delete_part(db, pack_id, part_id)
        return success(message="Part deleted")

    # --- Stems ---

    @router.post(
        "/{pack_id}/stems",
        summary="Upload one stem",
        description=(
            "Uploads a single instrument's audio as a 44.1/48 kHz WAV. `part_id` is required "
            "for a breakdown pack and rejected for a long-form one. The stem starts as "
            "`processing`; encryption, preview and duration are done in the background."
        ),
    )
    @limiter.limit("20/minute")
    async def add_stem(
        request: Request,
        pack_id: uuid.UUID,
        file: UploadFile = File(...),
        instrument: StemInstrument = Form(...),
        label: str | None = Form(None),
        part_id: uuid.UUID | None = Form(None),
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        data = StemCreate(instrument=instrument, label=label, part_id=part_id)
        stem = await stem_pack_service.add_stem_to_pack(db, pack_id, file, data)
        process_stem_upload.delay(str(stem.id))
        return success(
            StemResponse.model_validate(stem).model_dump(mode="json"), "Stem upload queued"
        )

    @router.post(
        "/{pack_id}/stems/bulk",
        summary="Upload a whole part's stems at once",
        description=(
            "Uploads several instruments in one request — the usual way to fill in a "
            "breakdown part, or a long-form pack in a single call. `instruments` is a "
            "comma-separated list matching the order of `files`; `labels` is optional and "
            "matched the same way. All files land in the same `part_id`."
        ),
    )
    @limiter.limit("10/minute")
    async def add_stems_bulk(
        request: Request,
        pack_id: uuid.UUID,
        files: list[UploadFile] = File(...),
        instruments: str = Form(..., description="e.g. `drums,piano,bass_guitar`"),
        labels: str | None = Form(None, description="Optional display names, same order"),
        part_id: uuid.UUID | None = Form(None),
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        parsed = _parse_instruments(instruments)
        if len(files) != len(parsed):
            raise AppError(
                f"Got {len(files)} file(s) but {len(parsed)} instrument(s); counts must match",
                status_code=422,
            )
        label_list = _parse_csv(labels)
        if label_list and len(label_list) != len(files):
            raise AppError(
                f"Got {len(files)} file(s) but {len(label_list)} label(s); counts must match",
                status_code=422,
            )

        stems_data = [
            StemCreate(
                instrument=inst,
                label=label_list[i] if label_list else None,
                part_id=part_id,
            )
            for i, inst in enumerate(parsed)
        ]
        stems = await stem_pack_service.add_stems_to_pack(db, pack_id, files, stems_data)
        for stem in stems:
            process_stem_upload.delay(str(stem.id))
        return success(
            [StemResponse.model_validate(s).model_dump(mode="json") for s in stems],
            f"{len(stems)} stem(s) upload queued",
        )

    @router.patch(
        "/{pack_id}/stems/{stem_id}",
        summary="Replace a stem's audio",
        description=(
            "Swaps the audio behind an existing stem, keeping its instrument and part. "
            "Any rendered arrangement built on this pack is marked `stale`."
        ),
    )
    @limiter.limit("20/minute")
    async def replace_stem_audio(
        request: Request,
        pack_id: uuid.UUID,
        stem_id: uuid.UUID,
        file: UploadFile = File(...),
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        stem = await stem_pack_service.replace_stem_audio(db, pack_id, stem_id, file)
        process_stem_upload.delay(str(stem.id))
        return success(
            {"stem_id": str(stem.id), "status": stem.status}, "Stem audio replacement queued"
        )

    @router.delete("/{pack_id}/stems/{stem_id}", summary="Delete a stem")
    @limiter.limit("20/minute")
    async def delete_stem(
        request: Request,
        pack_id: uuid.UUID,
        stem_id: uuid.UUID,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        await stem_pack_service.delete_stem(db, pack_id, stem_id)
        return success(message="Stem deleted")

    @router.post(
        "/{pack_id}/publish",
        summary="Publish the pack",
        description=(
            "Marks the pack as live and queues it for the next new-content digest — "
            "one email a day listing everything that went live, rather than an email "
            "per item. Publishing twice does not announce it twice."
        ),
    )
    @limiter.limit("5/minute")
    async def publish_stem_pack(
        request: Request,
        pack_id: uuid.UUID,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        pack = await owned(db, pack_id, actor)
        status = await stem_pack_service.upload_status(db, pack_id)
        if status["status"] != "ready":
            raise AppError(
                f"Pack is {status['status']} — every stem must finish processing first",
                status_code=409,
            )
        published_at = await stem_pack_service.publish(db, pack_id)
        return success(
            {"id": str(pack.id), "published_at": published_at.isoformat()},
            "Stem pack published — it will feature in the next digest",
        )

    # --- Arrangements ---

    @router.post(
        "/{pack_id}/arrangements",
        summary="Create an arrangement",
        description=(
            "Defines the order a breakdown pack's parts play in — intro, verse, chorus, "
            "chorus again — and queues a render. Rendering stitches each instrument's "
            "parts into one continuous file, inserting silence where an instrument sits a "
            "part out, and that rendered set is what buyers download. Every stem in the "
            "referenced parts must have finished processing."
        ),
    )
    @limiter.limit("20/minute")
    async def create_arrangement(
        request: Request,
        pack_id: uuid.UUID,
        body: StemArrangementCreate,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        arrangement = await stem_arrangement_service.create_arrangement(
            db, pack_id, body, actor.id
        )
        render_stem_arrangement.delay(str(arrangement.id))
        return success(
            stem_arrangement_service.payload(arrangement),
            "Arrangement created, render queued",
        )

    @router.get("/{pack_id}/arrangements", summary="List arrangements")
    @limiter.limit("60/minute")
    async def list_arrangements(
        request: Request,
        pack_id: uuid.UUID,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        arrangements = await stem_arrangement_service.list_arrangements(db, pack_id)
        return success([stem_arrangement_service.payload(a) for a in arrangements])

    @router.put(
        "/{pack_id}/arrangements/{arrangement_id}",
        summary="Update an arrangement",
        description=(
            "Sending `items` rewrites the running order and queues a fresh render; "
            "omitting them edits name, description or default flag only."
        ),
    )
    @limiter.limit("20/minute")
    async def update_arrangement(
        request: Request,
        pack_id: uuid.UUID,
        arrangement_id: uuid.UUID,
        body: StemArrangementUpdate,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        arrangement, should_render = await stem_arrangement_service.update_arrangement(
            db, pack_id, arrangement_id, body
        )
        if should_render:
            render_stem_arrangement.delay(str(arrangement.id))
        return success(
            stem_arrangement_service.payload(arrangement),
            "Arrangement re-render queued" if should_render else "Arrangement updated",
        )

    @router.post(
        "/{pack_id}/arrangements/{arrangement_id}/render",
        summary="Re-render an arrangement",
        description="Renders the same running order again — what a `stale` arrangement needs.",
    )
    @limiter.limit("10/minute")
    async def rerender_arrangement(
        request: Request,
        pack_id: uuid.UUID,
        arrangement_id: uuid.UUID,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        arrangement = await stem_arrangement_service.rerender(db, pack_id, arrangement_id)
        render_stem_arrangement.delay(str(arrangement.id))
        return success(stem_arrangement_service.payload(arrangement), "Render queued")

    @router.delete("/{pack_id}/arrangements/{arrangement_id}", summary="Delete an arrangement")
    @limiter.limit("20/minute")
    async def delete_arrangement(
        request: Request,
        pack_id: uuid.UUID,
        arrangement_id: uuid.UUID,
        db: AsyncSession = Depends(get_db),
        actor=Depends(actor_dependency),
    ):
        await owned(db, pack_id, actor)
        await stem_arrangement_service.delete_arrangement(db, pack_id, arrangement_id)
        return success(message="Arrangement deleted")

    return router
