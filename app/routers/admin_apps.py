import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth_middleware import require_admin
from app.middleware.rate_limit import limiter
from app.models.user import User
from app.schemas.app_download import DesktopAppAdmin, DesktopAppCreate, DesktopAppUpdate
from app.schemas.common import success
from app.services import app_download_service

router = APIRouter(prefix="/admin/apps", tags=["admin"])

_ADMIN_ERRORS = {
    401: {"description": "Missing or invalid token"},
    403: {"description": "Admin role required"},
}


def _dump(app) -> dict:
    return DesktopAppAdmin.model_validate(app).model_dump(mode="json")


@router.post(
    "",
    summary="Publish a downloadable app",
    description=(
        "Registers an installer users can request by `app_name` at "
        "`POST /app/download-request`.\n\n"
        "`file_url` is where the installer lives: `r2://<bucket>/<key>` or "
        "`s3://<bucket>/<key>` for a private object (served through a 5-minute "
        "presigned URL), or a plain `https://` URL.\n\n"
        "A paid app needs `price` and `currency` (`NGN` or `USD`); requesting it "
        "returns a checkout URL and the 3-day link is emailed once payment succeeds."
    ),
    status_code=201,
    responses={
        **_ADMIN_ERRORS,
        409: {"description": "An app with this name already exists for this OS"},
        422: {"description": "Invalid file_url, or a paid app without price/currency"},
    },
)
@limiter.limit("60/minute")
async def create_app(
    request: Request,
    body: DesktopAppCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    app = await app_download_service.create_app(db, body)
    return success(_dump(app), "App created")


@router.get("", summary="List published apps", responses=_ADMIN_ERRORS)
@limiter.limit("60/minute")
async def list_apps(
    request: Request,
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    apps = await app_download_service.list_apps(db, active_only=active_only)
    return success([_dump(a) for a in apps])


@router.get(
    "/{app_id}",
    summary="Get a published app",
    responses={**_ADMIN_ERRORS, 404: {"description": "App not found"}},
)
@limiter.limit("60/minute")
async def get_app(
    request: Request,
    app_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    return success(_dump(await app_download_service.get_app(db, app_id)))


@router.patch(
    "/{app_id}",
    summary="Update a published app",
    description=(
        "Partial update. Set `is_active` to false to stop new requests while "
        "keeping links already sent working."
    ),
    responses={
        **_ADMIN_ERRORS,
        404: {"description": "App not found"},
        409: {"description": "An app with this name already exists for this OS"},
        422: {"description": "The update would leave the app invalid"},
    },
)
@limiter.limit("60/minute")
async def update_app(
    request: Request,
    app_id: uuid.UUID,
    body: DesktopAppUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    app = await app_download_service.update_app(db, app_id, body)
    return success(_dump(app), "App updated")


@router.delete(
    "/{app_id}",
    summary="Delete a published app",
    description="Links already sent for this app stop working (410).",
    responses={**_ADMIN_ERRORS, 404: {"description": "App not found"}},
)
@limiter.limit("60/minute")
async def delete_app(
    request: Request,
    app_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    await app_download_service.delete_app(db, app_id)
    return success(None, "App deleted")
