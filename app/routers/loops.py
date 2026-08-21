from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
import httpx
from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.services import (
    loop_service, s3_service, like_service, free_tier_service, monthly_quota_service,
)
from app.models.monthly_download_usage import MonthlyQuotaType
from app.schemas.loop import (
    TIME_SIGNATURE_QUERY_PATTERN, LoopFilter, LoopResponse,
)
from app.schemas.common import success
from app.models.loop import Genre, TempoFeel
import uuid

router = APIRouter(prefix="/loops", tags=["loops"])


@router.get(
    "",
    summary="List loops",
    description=(
        "Paginated loops. `search` is free text across **title, description and "
        "genre** — searching `worship` finds a worship-genre loop whose title never "
        "says the word. Genre matching ignores case and punctuation, so `lofi` finds "
        "Lo-fi and `hip hop` finds Hip Hop.\n\n"
        "Every other parameter is an exact filter and is ANDed with the search: "
        "`?search=piano&genre=Trap` means a piano match *within* Trap. `tags` "
        "is the one many-valued filter — a loop matches when it carries any of "
        "the tags listed."
    ),
)
async def list_loops(
    search: str | None = Query(
        None, description="Free text over title, description and genre"
    ),
    genre: Genre | None = None,
    bpm_min: int | None = None,
    bpm_max: int | None = None,
    key: str | None = None,
    time_signature: str | None = Query(
        None,
        pattern=TIME_SIGNATURE_QUERY_PATTERN,
        description="Exact time signature, e.g. `4/4` or `6/8`",
    ),
    tempo_feel: TempoFeel | None = Query(
        None, description="`slow`, `mid` or `fast`"
    ),
    is_free: bool | None = None,
    tags: str | None = Query(
        None,
        description=(
            "Comma-separated tags, e.g. `808,dark`. A loop matches when it "
            "carries **any** of them."
        ),
    ),
    sort: str = "newest",
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    filters = LoopFilter(
        ready_only=True,
        search=search, genre=genre, bpm_min=bpm_min, bpm_max=bpm_max,
        key=key, time_signature=time_signature, tempo_feel=tempo_feel,
        is_free=is_free,
        tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None,
        sort=sort, page=page, page_size=page_size,
    )
    loops, total = await loop_service.list_loops(db, filters)
    return success({
        "items": [LoopResponse.model_validate(l).model_dump() for l in loops],
        "total": total,
        "page": filters.page,
        "page_size": filters.page_size,
    })


@router.get("/{loop_id}")
async def get_loop(loop_id: uuid.UUID, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    loop = await loop_service.get_loop(db, loop_id)
    return success(LoopResponse.model_validate(loop).model_dump())


@router.get("/{loop_id}/preview")
async def stream_preview(loop_id: uuid.UUID, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    loop = await loop_service.get_loop(db, loop_id)
    # Same reprocessing window as the download path — preview_s3_key is NULL
    # until the Celery job republishes the mp3.
    loop_service.assert_loop_ready(loop)
    url = await s3_service.generate_presigned_url(loop.preview_s3_key, expiry_seconds=300)
    async def _stream():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url) as resp:
                async for chunk in resp.aiter_bytes(8192):
                    yield chunk
    return StreamingResponse(_stream(), media_type="audio/mpeg")


@router.post("/{loop_id}/play")
async def record_play(loop_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await loop_service.increment_play_count(db, loop_id)
    return success(message="Play recorded")


@router.get("/{loop_id}/download")
async def download_loop(
    loop_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    from datetime import datetime, timedelta, timezone
    from app.models.download import Download

    loop = await loop_service.get_loop(db, loop_id)
    await loop_service.check_download_entitlement(db, user, loop)
    # Before enforce_loop_cap: a free-tier slot is a lifetime grant, so it must
    # not be consumed for a loop that cannot actually be downloaded.
    loop_service.assert_loop_ready(loop)
    await free_tier_service.enforce_loop_cap(db, user, loop)
    await monthly_quota_service.enforce(
        db, user, MonthlyQuotaType.loop, str(loop.id)
    )

    download_url = await s3_service.get_download_url(loop.file_s3_key, expiry_seconds=900)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

    dl = Download(
        user_id=user.id,
        loop_id=loop.id,
        download_url=download_url,
        expires_at=expires_at,
    )
    db.add(dl)
    loop.download_count += 1
    await db.commit()

    return success({
        "signed_url": download_url,
        "aes_key": loop.aes_key,
        "aes_iv": loop.aes_iv,
        "expires_in_seconds": 900,
    })


@router.post("/{loop_id}/like")
async def like_loop(
    loop_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    await like_service.like_loop(db, user.id, loop_id)
    return success(message="Loop liked")


@router.delete("/{loop_id}/like")
async def unlike_loop(
    loop_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    await like_service.unlike_loop(db, user.id, loop_id)
    return success(message="Loop unliked")
