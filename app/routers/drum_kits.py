import asyncio
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
import uuid

from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.services import (
    drum_kit_service, s3_service, cache_service, free_tier_service, monthly_quota_service,
)
from app.schemas.drum_kit import (
    DrumKitFilter, DrumKitResponse,
    DrumKitDownloadResponse, DrumSampleDownloadItem, DOWNLOAD_EXPIRY_SECONDS,
)
from app.schemas.common import success
from app.models.drum_kit import DrumKit
from app.models.monthly_download_usage import MonthlyQuotaType
from app.models.download import Download
from app.models.purchase import Purchase
from app.exceptions import NotFoundError, EntitlementError, AppError

router = APIRouter(prefix="/drum-kits", tags=["drum-kits"])

_401 = {"description": "Missing or invalid token"}
_403 = {"description": "Valid token but no purchase for this paid kit"}
_404 = {"description": "Drum kit not found"}
_409 = {"description": "No ready samples available yet — still processing"}


def _list_cache_key(search, is_free, tags, page, page_size) -> str:
    return f"drum_kit:list:{search}:{is_free}:{tags}:{page}:{page_size}"


async def _sample_preview_url(sample) -> str | None:
    if sample.preview_s3_key:
        return await s3_service.get_download_url(sample.preview_s3_key)
    return None


async def _kit_to_dict(kit) -> dict:
    data = DrumKitResponse.model_validate(kit).model_dump(mode="json")
    if kit.thumbnail_s3_key:
        data["thumbnail_url"] = await s3_service.get_download_url(kit.thumbnail_s3_key)
    preview_urls = await asyncio.gather(*[_sample_preview_url(s) for s in kit.samples])
    for sample_data, preview_url in zip(data["samples"], preview_urls):
        sample_data["preview_url"] = preview_url
    return data


@router.get(
    "",
    summary="List drum kits",
    description="Returns a paginated list of drum kits.",
    response_description="Paginated drum kit list",
)
async def list_drum_kits(
    search: str | None = Query(None, description="Case-insensitive title search"),
    is_free: bool | None = Query(None, description="Filter by free (`true`) or paid (`false`)"),
    tags: str | None = Query(None, description="Comma-separated tags, e.g. `trap,808`"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Results per page (max 100)"),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    cache_key = _list_cache_key(search, is_free, tags, page, page_size)

    async def _fetch():
        filters = DrumKitFilter(
            ready_only=True,
            search=search,
            is_free=is_free,
            tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None,
            page=page,
            page_size=page_size,
        )
        kits, total = await drum_kit_service.list_drum_kits(db, filters)
        return {
            "items": list(await asyncio.gather(*[_kit_to_dict(k) for k in kits])),
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    data = await cache_service.get_or_set(cache_key, _fetch, cache_service.TTL_DRUM_KIT_LIST)
    return success(data)


@router.get(
    "/{kit_id}",
    summary="Get drum kit detail",
    description="Returns a single drum kit with all its samples.",
    response_description="Drum kit detail with samples",
    responses={404: _404},
)
async def get_drum_kit(kit_id: uuid.UUID, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    cache_key = f"drum_kit:detail:{kit_id}"

    async def _fetch():
        result = await db.execute(
            select(DrumKit)
            .options(selectinload(DrumKit.samples))
            .where(DrumKit.id == kit_id)
        )
        kit = result.scalar_one_or_none()
        if not kit:
            raise NotFoundError(f"Drum kit {kit_id} not found")
        return await _kit_to_dict(kit)

    data = await cache_service.get_or_set(cache_key, _fetch, cache_service.TTL_DRUM_KIT_DETAIL)
    return success(data)


@router.get(
    "/{kit_id}/download",
    summary="Download drum kit",
    description=(
        "Returns signed S3 URLs and AES decryption keys for all ready samples. "
        "**Auth required.** For paid kits, the user must have a purchase record. "
        "Signed URLs expire in **15 minutes**."
    ),
    response_description="Signed download URLs and AES keys for each sample",
    responses={401: _401, 403: _403, 404: _404, 409: _409},
)
async def download_drum_kit(
    kit_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    result = await db.execute(
        select(DrumKit)
        .options(selectinload(DrumKit.samples))
        .where(DrumKit.id == kit_id)
    )
    kit = result.scalar_one_or_none()
    if not kit:
        raise NotFoundError(f"Drum kit {kit_id} not found")

    if not kit.is_free:
        purchase = await db.scalar(
            select(Purchase).where(
                Purchase.user_id == user.id,
                Purchase.drum_kit_id == kit.id,
            )
        )
        if not purchase:
            raise EntitlementError()

    sample_items: list[DrumSampleDownloadItem] = []
    for sample in kit.samples:
        if sample.status != "ready" or not sample.file_s3_key:
            continue
        signed_url = await s3_service.get_download_url(
            sample.file_s3_key, expiry_seconds=DOWNLOAD_EXPIRY_SECONDS
        )
        sample_items.append(DrumSampleDownloadItem(
            id=sample.id,
            label=sample.label,
            signed_url=signed_url,
            aes_key=sample.aes_key,
            aes_iv=sample.aes_iv,
            duration=sample.duration,
        ))

    if not sample_items:
        raise AppError("No ready samples available for download yet", status_code=409)

    await free_tier_service.enforce_drum_kit_cap(db, user, kit)
    await monthly_quota_service.enforce(
        db, user, MonthlyQuotaType.drum_kit, str(kit.id)
    )

    dl = Download(
        user_id=user.id,
        drum_kit_id=kit.id,
        download_url=f"drum-kit:{kit.id}",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=DOWNLOAD_EXPIRY_SECONDS),
    )
    db.add(dl)
    kit.download_count += 1
    await db.commit()

    return success(DrumKitDownloadResponse(
        kit_id=kit.id,
        title=kit.title,
        samples=sample_items,
    ).model_dump())
