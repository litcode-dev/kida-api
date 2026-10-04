from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.services import like_service, loop_service
from app.schemas.stem_pack import StemPackResponse
from app.schemas.common import success

router = APIRouter(prefix="/likes", tags=["likes"])


@router.get("/loops")
async def get_liked_loops(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    loops, total = await like_service.get_liked_loops(db, user.id, page, page_size)
    return success({
        "items": await loop_service.serialize_loops(db, loops),
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get("/stem-packs")
async def get_liked_stem_packs(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    packs, total = await like_service.get_liked_stem_packs(db, user.id, page, page_size)
    return success({
        "items": [StemPackResponse.model_validate(p).model_dump() for p in packs],
        "total": total,
        "page": page,
        "page_size": page_size,
    })
