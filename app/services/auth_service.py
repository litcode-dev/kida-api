import asyncio
import uuid
from datetime import datetime, timedelta, timezone
import jwt
import bcrypt
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.config import get_settings
from app.models.user import User, UserRole
from app.exceptions import UnauthorizedError, ConflictError

settings = get_settings()

ALGORITHM = "HS256"
REFRESH_PREFIX = "refresh:"


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(
        lambda: bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    )


async def verify_password(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(
        lambda: bcrypt.checkpw(plain.encode(), hashed.encode())
    )


def create_access_token(user_id: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": user_id, "role": role, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def create_refresh_token() -> str:
    return str(uuid.uuid4())


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Token has expired")
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid or expired token")


async def store_refresh_token(redis: Redis, token: str, user_id: str) -> None:
    key = f"{REFRESH_PREFIX}{token}"
    expire_seconds = settings.refresh_token_expire_days * 86400
    await redis.setex(key, expire_seconds, user_id)


async def validate_refresh_token(redis: Redis, token: str) -> str:
    key = f"{REFRESH_PREFIX}{token}"
    user_id = await redis.get(key)
    if not user_id:
        raise UnauthorizedError("Refresh token invalid or expired")
    return user_id.decode() if isinstance(user_id, bytes) else user_id


async def revoke_refresh_token(redis: Redis, token: str) -> None:
    await redis.delete(f"{REFRESH_PREFIX}{token}")


async def register_user(db: AsyncSession, email: str, password: str, full_name: str) -> User:
    existing = await db.scalar(select(User).where(User.email == email))
    if existing:
        raise ConflictError("Email already registered")
    user = User(
        email=email,
        password_hash=await hash_password(password),
        full_name=full_name,
        role=UserRole.user,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    from app.tasks.notification_tasks import send_registration_email
    send_registration_email.delay(str(user.id))
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User:
    user = await db.scalar(select(User).where(User.email == email))
    if not user or not user.password_hash or not await verify_password(password, user.password_hash):
        raise UnauthorizedError("Invalid credentials")
    return user


async def find_or_create_oauth_user(
    db: AsyncSession,
    email: str,
    full_name: str,
    provider: str,
    provider_id: str,
    avatar_url: str | None = None,
) -> User:
    # First: find by provider + provider_id
    user = await db.scalar(
        select(User).where(
            User.oauth_provider == provider,
            User.oauth_provider_id == provider_id,
        )
    )
    if user:
        if avatar_url and user.avatar_url != avatar_url:
            user.avatar_url = avatar_url
            await db.commit()
            await db.refresh(user)
        return user

    # Second: find by email and link the OAuth account
    user = await db.scalar(select(User).where(User.email == email))
    if user:
        user.oauth_provider = provider
        user.oauth_provider_id = provider_id
        if avatar_url:
            user.avatar_url = avatar_url
        await db.commit()
        await db.refresh(user)
        return user

    # Third: create new OAuth-only user
    user = User(
        email=email,
        full_name=full_name,
        password_hash=None,
        role=UserRole.user,
        avatar_url=avatar_url,
        oauth_provider=provider,
        oauth_provider_id=provider_id,
    )
    db.add(user)
    try:
        await db.commit()
        await db.refresh(user)
        from app.tasks.notification_tasks import send_registration_email
        send_registration_email.delay(str(user.id))
        return user
    except IntegrityError:
        await db.rollback()
        user = await db.scalar(select(User).where(User.email == email))
        if not user:
            raise
        if not user.oauth_provider:
            user.oauth_provider = provider
            user.oauth_provider_id = provider_id
            if avatar_url:
                user.avatar_url = avatar_url
            await db.commit()
            await db.refresh(user)
        return user


async def get_user_by_id(db: AsyncSession, user_id: str) -> User:
    import uuid as _uuid
    user = await db.get(User, _uuid.UUID(user_id))
    if not user:
        raise UnauthorizedError("User not found")
    return user
