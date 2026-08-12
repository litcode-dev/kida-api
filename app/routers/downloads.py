from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.services import download_service, monthly_quota_service
from app.schemas.download import DownloadedLoopItem
from app.schemas.common import success

router = APIRouter(prefix="/downloads", tags=["downloads"])


@router.get("")
async def get_download_history(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Return all distinct loops the authenticated user has downloaded."""
    rows, total = await download_service.get_user_download_history(
        db, user.id, page, page_size
    )
    items = [DownloadedLoopItem.model_validate(dict(row)).model_dump() for row in rows]
    return success({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get(
    "/quota",
    summary="Monthly download allowance",
    description=(
        "How much of this month's download allowance the authenticated user has "
        "spent, per item type, with the UTC instant it resets.\n\n"
        "Counted per distinct item: re-downloading something already taken this "
        "month does not spend another slot, and a drone group counts once however "
        "many of its pads are fetched.\n\n"
        "A type with no cap reports `limit` and `remaining` as the string "
        "`\"unlimited\"`; branch on the `unlimited` boolean rather than "
        "comparing strings."
    ),
    responses={401: {"description": "Missing or invalid token"}},
)
async def get_monthly_quota(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    return success(await monthly_quota_service.quota_summary(db, user.id))
