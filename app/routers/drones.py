from fastapi import APIRouter, Depends
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
async def list_drone_categories(db: AsyncSession = Depends(get_db)):
    cached = await cache_service.get("drone:categories")
    if cached is not None:
        return success(cached)
    categories = await drone_service.list_categories(db)
    data = [DronePadCategoryResponse.model_validate(c).model_dump(mode="json") for c in categories]
    await cache_service.set("drone:categories", data, cache_service.TTL_DRONE_CATEGORIES)
    return success(data)


@router.get("/categories/{category_id}")
async def get_drone_category(category_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    cache_key = f"drone:category:{category_id}"
    cached = await cache_service.get(cache_key)
    if cached is not None:
        return success(cached)
    category = await drone_service.get_category(db, category_id)
    data = DronePadCategoryResponse.model_validate(category).model_dump(mode="json")
    await cache_service.set(cache_key, data, cache_service.TTL_DRONE_CATEGORIES)
    return success(data)


@router.get("")
async def list_drones(
    key: MusicalKey | None = None,
    is_free: bool | None = None,
    category_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
):
    cache_key = (
        f"drone:list:{key.value if key else 'none'}"
        f":{str(is_free).lower() if is_free is not None else 'none'}"
        f":{category_id or 'none'}"
        f":{page}:{page_size}"
    )
    cached = await cache_service.get(cache_key)
    if cached is not None:
        return success(cached)

    filters = DronePadFilter(
        key=key, is_free=is_free, category_id=category_id,
        page=page, page_size=page_size,
    )
    drones, total = await drone_service.list_drones(db, filters)
    data = {
        "items": [DroneResponse.model_validate(d).model_dump(mode="json") for d in drones],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    await cache_service.set(cache_key, data, cache_service.TTL_DRONE_LIST)
    return success(data)


@router.get("/{drone_id}")
async def get_drone(drone_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    drone = await drone_service.get_drone(db, drone_id)
    return success(DroneResponse.model_validate(drone).model_dump(mode="json"))


@router.get("/{drone_id}/preview")
async def stream_preview(drone_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
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
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    items = await drone_service.get_drone_downloads(db, user, drone_id)
    return success({"items": items, "total": len(items)})
