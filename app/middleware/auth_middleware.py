from fastapi import Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from app.database import get_db
from app.services.auth_service import decode_access_token, get_user_by_id
from app.models.user import ADMIN_ROLES, User, UserRole
from app.exceptions import UnauthorizedError, ForbiddenError

bearer = HTTPBearer()


async def get_redis() -> Redis:
    from app.config import get_settings
    settings = get_settings()
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield r
    finally:
        await r.aclose()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> User:
    token = credentials.credentials
    payload = decode_access_token(token)
    user = await get_user_by_id(db, payload["sub"])
    try:
        redis_reason = await redis.get(f"suspended:{user.id}")
    except Exception:
        redis_reason = None
    if redis_reason or user.is_suspended:
        reason = redis_reason or user.suspension_reason
        msg = f"Account suspended: {reason}" if reason else "Account suspended"
        raise UnauthorizedError(msg)
    if user.deleted_at is not None:
        # Access tokens outlive the delete request by up to their expiry, so the
        # check has to live here rather than only at login.
        raise UnauthorizedError("Account pending deletion")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Any admin-level role. A super admin can do everything an admin can."""
    if user.role not in ADMIN_ROLES:
        raise ForbiddenError("Admin access required")
    return user


async def require_super_admin(user: User = Depends(get_current_user)) -> User:
    """The elevated actions: anything that touches another admin."""
    if user.role != UserRole.super_admin:
        raise ForbiddenError("Super admin access required")
    return user


async def require_producer(user: User = Depends(get_current_user)) -> User:
    if user.role not in (UserRole.producer, *ADMIN_ROLES):
        raise ForbiddenError("Producer or admin access required")
    return user
