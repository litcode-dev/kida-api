from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.services import stem_pack_service, s3_service, like_service
from app.models.stem_pack import Stem
from app.schemas.stem_pack import StemPackResponse
from app.schemas.common import success
from app.models.download import Download
from datetime import datetime, timedelta, timezone
import uuid

router = APIRouter(prefix="/stem-packs", tags=["stem-packs"])


@router.get("")
async def list_stem_packs(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    from sqlalchemy import func, select
    from app.models.stem_pack import StemPack
    count_q = select(func.count()).select_from(select(StemPack).subquery())
    total = await db.scalar(count_q)
    q = select(StemPack).offset((page - 1) * page_size).limit(page_size)
    packs = await db.scalars(q)
    return success({
        "items": [StemPackResponse.model_validate(p).model_dump() for p in packs.all()],
        "total": total or 0,
        "page": page,
        "page_size": page_size,
    })


@router.get("/{pack_id}")
async def get_stem_pack(pack_id: uuid.UUID, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    pack = await stem_pack_service.get_stem_pack_with_stems(db, pack_id)
    return success(StemPackResponse.model_validate(pack).model_dump())


@router.get("/{pack_id}/download")
async def download_stem_pack(
    pack_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    await stem_pack_service.check_stem_pack_entitlement(db, user, pack_id)
    stems = await db.scalars(select(Stem).where(Stem.stem_pack_id == pack_id))
    stem_list = list(stems.all())

    download_links = []
    for stem in stem_list:
        download_url = await s3_service.get_download_url(stem.file_s3_key, expiry_seconds=900)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        dl = Download(
            user_id=user.id,
            stem_id=stem.id,
            download_url=download_url,
            expires_at=expires_at,
        )
        db.add(dl)
        download_links.append({
            "stem_id": str(stem.id),
            "label": stem.label,
            "signed_url": download_url,
            "aes_key": stem.aes_key,
            "aes_iv": stem.aes_iv,
            "expires_in_seconds": 900,
        })

    await db.commit()
    return success({"stems": download_links})


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
