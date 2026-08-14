import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.exceptions import AppError
from app.middleware.auth_middleware import get_current_user
from app.models.download import Download
from app.models.loop import Genre
from app.models.stem_pack import Stem, StemPackType, StemPart
from app.schemas.common import success
from app.schemas.stem_pack import (
    DOWNLOAD_EXPIRY_SECONDS,
    ArrangementTrackDownloadItem,
    StemArrangementDownloadResponse,
    StemDownloadItem,
    StemPackDownloadResponse,
    StemPackFilter,
    StemPackResponse,
)
from app.services import (
    like_service, s3_service, stem_arrangement_service, stem_pack_service,
)

router = APIRouter(prefix="/stem-packs", tags=["stem-packs"])

_401 = {"description": "Missing or invalid token"}
_403 = {"description": "Valid token but no purchase for this pack"}
_404 = {"description": "Stem pack not found"}
_409 = {"description": "Nothing ready to download yet — still processing"}


async def _preview_url(item) -> str | None:
    if item.preview_s3_key:
        return await s3_service.get_download_url(item.preview_s3_key)
    return None


async def _arrangement_to_dict(arrangement) -> dict:
    data = stem_arrangement_service.payload(arrangement)
    preview_urls = await asyncio.gather(*[_preview_url(t) for t in arrangement.tracks])
    for track_data, preview_url in zip(data["tracks"], preview_urls):
        track_data["preview_url"] = preview_url
    return data


async def _pack_to_dict(pack) -> dict:
    data = StemPackResponse.model_validate(pack).model_dump(mode="json")

    # A breakdown pack's stems appear both loose on the pack and under their
    # part; keyed by id, each one is signed once however many times it shows up.
    stems = {s.id: s for s in list(pack.stems) + [s for p in pack.parts for s in p.stems]}
    preview_urls = dict(zip(
        stems.keys(),
        await asyncio.gather(*[_preview_url(s) for s in stems.values()]),
    ))
    for stem_data in data["stems"]:
        stem_data["preview_url"] = preview_urls.get(uuid.UUID(str(stem_data["id"])))
    for part_data in data["parts"]:
        for stem_data in part_data["stems"]:
            stem_data["preview_url"] = preview_urls.get(uuid.UUID(str(stem_data["id"])))

    data["arrangements"] = list(await asyncio.gather(
        *[_arrangement_to_dict(a) for a in pack.arrangements]
    ))
    return data


@router.get(
    "",
    summary="List stem packs",
    description=(
        "Paginated stem packs. `pack_type=long_form` returns packs whose stems are "
        "one continuous take per instrument; `pack_type=breakdown` returns packs "
        "uploaded part by part, which carry their stems inside `parts` and their "
        "running orders inside `arrangements`.\n\n"
        "`search` is free text across **title, description and genre** — searching "
        "`worship` finds a worship-genre pack whose title never says the word. Genre "
        "matching ignores case and punctuation, so `lofi` finds Lo-fi. Every other "
        "parameter is an exact filter and ANDs with the search."
    ),
)
async def list_stem_packs(
    search: str | None = Query(
        None, description="Free text over title, description and genre"
    ),
    genre: Genre | None = Query(None),
    pack_type: StemPackType | None = Query(None, description="`long_form` or `breakdown`"),
    tags: str | None = Query(None, description="Comma-separated tags"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    filters = StemPackFilter(
        ready_only=True,
        search=search,
        genre=genre,
        pack_type=pack_type,
        tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None,
        page=page,
        page_size=page_size,
    )
    packs, total = await stem_pack_service.list_stem_packs(db, filters)
    return success({
        "items": list(await asyncio.gather(*[_pack_to_dict(p) for p in packs])),
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get(
    "/{pack_id}",
    summary="Get stem pack detail",
    description="A single pack with its stems, song parts and arrangements.",
    responses={404: _404},
)
async def get_stem_pack(
    pack_id: uuid.UUID, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    pack = await stem_pack_service.get_stem_pack_with_stems(db, pack_id)
    return success(await _pack_to_dict(pack))


@router.get(
    "/{pack_id}/arrangements",
    summary="List a pack's arrangements",
    description="The running orders defined for a breakdown pack, with their rendered tracks.",
    responses={404: _404},
)
async def list_arrangements(
    pack_id: uuid.UUID, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    arrangements = await stem_arrangement_service.list_arrangements(db, pack_id)
    return success(list(await asyncio.gather(
        *[_arrangement_to_dict(a) for a in arrangements]
    )))


@router.get(
    "/{pack_id}/download",
    summary="Download the pack's stems",
    description=(
        "Signed URLs and AES keys for every ready stem in the pack. **Auth required**, "
        "and the pack must have been purchased.\n\n"
        "For a long-form pack this is one file per instrument. For a breakdown pack it "
        "is every part's stems, tagged with the part they belong to — use "
        "`/arrangements/{arrangement_id}/download` for the stitched-together song instead. "
        "Signed URLs expire in **15 minutes**."
    ),
    responses={401: _401, 403: _403, 404: _404, 409: _409},
)
async def download_stem_pack(
    pack_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    pack = await stem_pack_service.get_stem_pack(db, pack_id)
    await stem_pack_service.check_stem_pack_entitlement(db, user, pack_id)

    rows = await db.execute(
        select(Stem, StemPart.name)
        .outerjoin(StemPart, Stem.part_id == StemPart.id)
        .where(Stem.stem_pack_id == pack_id)
        .order_by(StemPart.position, Stem.instrument)
    )

    items: list[StemDownloadItem] = []
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=DOWNLOAD_EXPIRY_SECONDS)
    for stem, part_name in rows:
        if stem.status != "ready" or not stem.file_s3_key:
            continue
        signed_url = await s3_service.get_download_url(
            stem.file_s3_key, expiry_seconds=DOWNLOAD_EXPIRY_SECONDS
        )
        items.append(StemDownloadItem(
            id=stem.id,
            instrument=stem.instrument,
            label=stem.label,
            signed_url=signed_url,
            aes_key=stem.aes_key,
            aes_iv=stem.aes_iv,
            duration=stem.duration,
            part_id=stem.part_id,
            part_name=part_name,
        ))
        db.add(Download(
            user_id=user.id,
            stem_id=stem.id,
            download_url=signed_url,
            expires_at=expires_at,
        ))

    if not items:
        raise AppError("No ready stems available for download yet", status_code=409)

    await db.commit()
    return success(StemPackDownloadResponse(
        pack_id=pack.id,
        title=pack.title,
        pack_type=pack.pack_type,
        stems=items,
    ).model_dump())


@router.get(
    "/{pack_id}/arrangements/{arrangement_id}/download",
    summary="Download an arranged song",
    description=(
        "Signed URLs and AES keys for a rendered arrangement — one continuous file per "
        "instrument, with the pack's parts already stitched into the running order. "
        "**Auth required**, and the pack must have been purchased. "
        "Signed URLs expire in **15 minutes**."
    ),
    responses={401: _401, 403: _403, 404: _404, 409: _409},
)
async def download_arrangement(
    pack_id: uuid.UUID,
    arrangement_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    await stem_pack_service.get_stem_pack(db, pack_id)
    await stem_pack_service.check_stem_pack_entitlement(db, user, pack_id)
    arrangement = await stem_arrangement_service.get_arrangement(db, pack_id, arrangement_id)

    tracks: list[ArrangementTrackDownloadItem] = []
    for track in sorted(arrangement.tracks, key=lambda t: t.instrument.value):
        if track.status != "ready" or not track.file_s3_key:
            continue
        signed_url = await s3_service.get_download_url(
            track.file_s3_key, expiry_seconds=DOWNLOAD_EXPIRY_SECONDS
        )
        tracks.append(ArrangementTrackDownloadItem(
            id=track.id,
            instrument=track.instrument,
            signed_url=signed_url,
            aes_key=track.aes_key,
            aes_iv=track.aes_iv,
            duration=track.duration,
        ))

    if not tracks:
        raise AppError(
            f"This arrangement is {arrangement.status} — no rendered tracks to download yet",
            status_code=409,
        )

    db.add(Download(
        user_id=user.id,
        download_url=f"stem-arrangement:{arrangement.id}",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=DOWNLOAD_EXPIRY_SECONDS),
    ))
    await db.commit()

    return success(StemArrangementDownloadResponse(
        pack_id=pack_id,
        arrangement_id=arrangement.id,
        name=arrangement.name,
        duration=arrangement.duration,
        tracks=tracks,
    ).model_dump())


@router.post("/{pack_id}/like")
async def like_stem_pack(
    pack_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    await like_service.like_stem_pack(db, user.id, pack_id)
    return success(message="Stem pack liked")


@router.delete("/{pack_id}/like")
async def unlike_stem_pack(
    pack_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    await like_service.unlike_stem_pack(db, user.id, pack_id)
    return success(message="Stem pack unliked")
