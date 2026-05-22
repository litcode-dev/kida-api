import asyncio
import uuid
from datetime import datetime, timedelta, timezone
import jwt
import bcrypt
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from app.config import get_settings
from app.models.user import User, UserRole
from app.exceptions import UnauthorizedError, ConflictError

settings = get_settings()
log = structlog.get_logger()

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
    if user.is_suspended:
        raise UnauthorizedError("Account suspended")
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
        log.info("registration_email.queued", user_id=str(user.id), email=user.email)
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


async def delete_user(db: AsyncSession, user: User, redis: Redis, refresh_token: str | None) -> None:
    from app.models.ai_generation import AIGeneration
    from app.models.download import Download
    from app.models.like import Like
    from app.models.purchase import Purchase
    from app.models.subscription import Subscription
    from app.models.loop import Loop
    from app.models.stem_pack import StemPack, Stem
    from app.models.drum_kit import DrumKit
    from app.models.drone_pad import DronePadCategory, Drone, DronePad

    if refresh_token:
        await revoke_refresh_token(redis, refresh_token)

    user_email = user.email
    user_full_name = user.full_name
    uid = user.id

    # Transactional records owned by the user
    await db.execute(delete(AIGeneration).where(AIGeneration.user_id == uid))
    await db.execute(delete(Download).where(Download.user_id == uid))
    await db.execute(delete(Like).where(Like.user_id == uid))
    await db.execute(delete(Purchase).where(Purchase.user_id == uid))
    await db.execute(delete(Subscription).where(Subscription.user_id == uid))

    # Producer content created by the user — delete children before parents
    loop_ids = (await db.scalars(select(Loop.id).where(Loop.created_by == uid))).all()
    if loop_ids:
        await db.execute(delete(Download).where(Download.loop_id.in_(loop_ids)))
        await db.execute(delete(Like).where(Like.loop_id.in_(loop_ids)))
        await db.execute(delete(Purchase).where(Purchase.loop_id.in_(loop_ids)))
        await db.execute(delete(Loop).where(Loop.id.in_(loop_ids)))

    stem_pack_ids = (await db.scalars(select(StemPack.id).where(StemPack.created_by == uid))).all()
    if stem_pack_ids:
        await db.execute(delete(Stem).where(Stem.stem_pack_id.in_(stem_pack_ids)))
        await db.execute(delete(Purchase).where(Purchase.stem_pack_id.in_(stem_pack_ids)))
        await db.execute(delete(Like).where(Like.stem_pack_id.in_(stem_pack_ids)))
        await db.execute(delete(StemPack).where(StemPack.id.in_(stem_pack_ids)))

    drum_kit_ids = (await db.scalars(select(DrumKit.id).where(DrumKit.created_by == uid))).all()
    if drum_kit_ids:
        await db.execute(delete(Download).where(Download.drum_kit_id.in_(drum_kit_ids)))
        await db.execute(delete(Purchase).where(Purchase.drum_kit_id.in_(drum_kit_ids)))
        await db.execute(delete(DrumKit).where(DrumKit.id.in_(drum_kit_ids)))

    drone_cat_ids = (await db.scalars(select(DronePadCategory.id).where(DronePadCategory.created_by == uid))).all()
    if drone_cat_ids:
        drone_ids = (await db.scalars(select(Drone.id).where(Drone.category_id.in_(drone_cat_ids)))).all()
        if drone_ids:
            drone_pad_ids = (await db.scalars(select(DronePad.id).where(DronePad.drone_id.in_(drone_ids)))).all()
            if drone_pad_ids:
                await db.execute(delete(Download).where(Download.drone_pad_id.in_(drone_pad_ids)))
                await db.execute(delete(Purchase).where(Purchase.drone_pad_id.in_(drone_pad_ids)))
                await db.execute(delete(DronePad).where(DronePad.id.in_(drone_pad_ids)))
            await db.execute(delete(Drone).where(Drone.id.in_(drone_ids)))
        await db.execute(delete(DronePadCategory).where(DronePadCategory.id.in_(drone_cat_ids)))

    await db.execute(delete(User).where(User.id == uid))
    await db.commit()

    from app.services.email_service import send_email, account_deleted_html, account_deleted_text
    await send_email(
        to=user_email,
        subject="Your Kida account has been deleted",
        html=account_deleted_html(user_full_name),
        text=account_deleted_text(user_full_name),
    )


async def get_user_by_id(db: AsyncSession, user_id: str) -> User:
    import uuid as _uuid
    user = await db.get(User, _uuid.UUID(user_id))
    if not user:
        raise UnauthorizedError("User not found")
    return user
