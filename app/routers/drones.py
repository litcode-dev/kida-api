from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
import httpx
import uuid

from app.database import get_db
from app.middleware.auth_middleware import get_current_user
from app.services import drone_service, s3_service, cache_service
from app.schemas.drone_pad import DronePadFilter, DroneResponse, DronePadCategoryResponse
from app.schemas.common import success
from app.models.drone_pad import MusicalKey

router = APIRouter(prefix="/drones", tags=["drones"])


@router.get("/categories")
async def list_drone_categories(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    async def _fetch():
        categories = await drone_service.list_categories(db)
        return [DronePadCategoryResponse.model_validate(c).model_dump(mode="json") for c in categories]

    data = await cache_service.get_or_set("drone:categories", _fetch, cache_service.TTL_DRONE_CATEGORIES)
    return success(data)


@router.get("/categories/{category_id}")
async def get_drone_category(category_id: uuid.UUID, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    cache_key = f"drone:category:{category_id}"

    async def _fetch():
        category = await drone_service.get_category(db, category_id)
        return DronePadCategoryResponse.model_validate(category).model_dump(mode="json")

    data = await cache_service.get_or_set(cache_key, _fetch, cache_service.TTL_DRONE_CATEGORIES)
    return success(data)


@router.get(
    "",
    summary="List drones",
    description=(
        "Paginated drones. `search` is free text across **title, description and "
        "category name** — a drone filed under Cinematic is findable by that word "
        "even when its own title never says it. Every other parameter is an exact "
        "filter and ANDs with the search."
    ),
)
async def list_drones(
    search: str | None = Query(
        None, description="Free text over title, description and category name"
    ),
    key: MusicalKey | None = None,
    is_free: bool | None = None,
    category_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    # search is part of the key: without it every search would be answered from
    # — and would poison — the unfiltered list's cache entry.
    cache_key = (
        f"drone:list:{search or 'none'}"
        f":{key.value if key else 'none'}"
        f":{str(is_free).lower() if is_free is not None else 'none'}"
        f":{category_id or 'none'}"
        f":{page}:{page_size}"
    )

    async def _fetch():
        filters = DronePadFilter(
            search=search, key=key, is_free=is_free, category_id=category_id,
            page=page, page_size=page_size,
        )
        drones, total = await drone_service.list_drones(db, filters)
        return {
            "items": [DroneResponse.model_validate(d).model_dump(mode="json") for d in drones],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    data = await cache_service.get_or_set(cache_key, _fetch, cache_service.TTL_DRONE_LIST)
    return success(data)


@router.get("/titles/{title}/download")
async def download_drones_by_title(
    title: str,
    key: MusicalKey | None = Query(
        None,
        description=(
            "Musical key of a single pad (URL-encoded, e.g. `C%23`). When present, "
            "only that pad is returned and one free-tier drone_pad grant is used. "
            "When absent, downloading a free group requires an active subscription."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    items = await drone_service.get_title_downloads(db, user, title, key)
    return success({"items": items, "total": len(items)})


@router.get("/{drone_id}")
async def get_drone(drone_id: uuid.UUID, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    drone = await drone_service.get_drone(db, drone_id)
    return success(DroneResponse.model_validate(drone).model_dump(mode="json"))


@router.get("/{drone_id}/preview")
async def stream_preview(drone_id: uuid.UUID, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    drone = await drone_service.get_drone(db, drone_id)
    pad = next((p for p in drone.pads if p.preview_s3_key), None)
    if pad is None:
        from app.exceptions import NotFoundError
        raise NotFoundError(f"Drone {drone_id} has no preview")
    url = await s3_service.generate_presigned_url(pad.preview_s3_key, expiry_seconds=300)

    async def _stream():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url) as resp:
                async for chunk in resp.aiter_bytes(8192):
                    yield chunk

    return StreamingResponse(_stream(), media_type="audio/mpeg")


@router.get("/{drone_id}/download")
async def download_drone(
    drone_id: uuid.UUID,
    key: MusicalKey | None = Query(
        None,
        description=(
            "Musical key of a single pad (URL-encoded, e.g. `C%23`). When present, "
            "only that pad is returned and one free-tier drone_pad grant is used. "
            "When absent, downloading a free group requires an active subscription."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    items = await drone_service.get_drone_downloads(db, user, drone_id, key)
    return success({"items": items, "total": len(items)})
