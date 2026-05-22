from fastapi import Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from app.database import get_db
from app.services.auth_service import decode_access_token, get_user_by_id
from app.models.user import User, UserRole
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
    if await redis.exists(f"suspended:{user.id}") or user.is_suspended:
        raise UnauthorizedError("Account suspended")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.admin:
        raise ForbiddenError("Admin access required")
    return user


async def require_producer(user: User = Depends(get_current_user)) -> User:
    if user.role not in (UserRole.producer, UserRole.admin):
        raise ForbiddenError("Producer or admin access required")
    return user
