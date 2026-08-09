import asyncio
import secrets
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
from app.models.newsletter import NewsletterSubscriber
from app.exceptions import UnauthorizedError, ConflictError, EmailNotVerifiedError, AppError

settings = get_settings()
log = structlog.get_logger()

ALGORITHM = "HS256"
REFRESH_PREFIX = "refresh:"
VERIFY_PREFIX = "email_verify:"
VERIFY_COOLDOWN_PREFIX = "email_verify_cooldown:"
VERIFY_CODE_TTL_MINUTES = 15
VERIFY_RESEND_COOLDOWN_SECONDS = 60


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


def _generate_verification_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def issue_verification_code(redis: Redis, user_id: str) -> str:
    """Generate a fresh 6-digit code and store it in Redis with a TTL."""
    code = _generate_verification_code()
    await redis.setex(f"{VERIFY_PREFIX}{user_id}", VERIFY_CODE_TTL_MINUTES * 60, code)
    return code


async def _cooldown_active(redis: Redis, user_id: str) -> bool:
    return bool(await redis.get(f"{VERIFY_COOLDOWN_PREFIX}{user_id}"))


async def _start_cooldown(redis: Redis, user_id: str) -> None:
    await redis.setex(f"{VERIFY_COOLDOWN_PREFIX}{user_id}", VERIFY_RESEND_COOLDOWN_SECONDS, "1")


async def register_user(
    db: AsyncSession, redis: Redis, email: str, password: str, full_name: str
) -> tuple[User, str]:
    """Create an unverified account and return it along with a verification code.

    The account cannot log in until the email is verified (see verify_email).
    """
    existing = await db.scalar(select(User).where(User.email == email))
    if existing:
        raise ConflictError("Email already registered")
    user = User(
        email=email,
        password_hash=await hash_password(password),
        full_name=full_name,
        role=UserRole.user,
        is_verified=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    code = await issue_verification_code(redis, str(user.id))
    # The registration email counts as the first send, so start the resend
    # cooldown now — this also stops an immediate login retry from firing a
    # duplicate code.
    await _start_cooldown(redis, str(user.id))
    log.info("verification_code.issued", user_id=str(user.id), email=user.email)
    return user, code


async def verify_email(db: AsyncSession, redis: Redis, email: str, code: str) -> User:
    """Confirm a registration code and mark the account verified."""
    user = await db.scalar(select(User).where(User.email == email))
    if not user:
        raise AppError("Invalid or expired verification code", status_code=400)
    if user.is_verified:
        return user

    key = f"{VERIFY_PREFIX}{user.id}"
    stored = await redis.get(key)
    if stored is None:
        raise AppError("Invalid or expired verification code", status_code=400)
    stored = stored.decode() if isinstance(stored, bytes) else stored
    if not secrets.compare_digest(stored, code):
        raise AppError("Invalid or expired verification code", status_code=400)

    user.is_verified = True
    await db.commit()
    await db.refresh(user)
    await redis.delete(key)
    await redis.delete(f"{VERIFY_COOLDOWN_PREFIX}{user.id}")

    from app.tasks.notification_tasks import (
        send_registration_email, send_new_user_admin_notification,
    )
    send_registration_email.delay(str(user.id))
    send_new_user_admin_notification.delay(str(user.id))
    log.info("email.verified", user_id=str(user.id), email=user.email)
    return user


async def resend_verification_code(
    db: AsyncSession, redis: Redis, email: str
) -> tuple[str, str] | None:
    """Issue a new verification code for an unverified account.

    Returns ``(user_id, code)`` when a fresh code was issued, or None when there
    is nothing to do (unknown email or already verified) so the caller can
    respond generically without revealing whether the email exists.
    """
    user = await db.scalar(select(User).where(User.email == email))
    if not user or user.is_verified:
        return None

    if await _cooldown_active(redis, str(user.id)):
        raise AppError("Please wait before requesting another verification code", status_code=429)

    code = await issue_verification_code(redis, str(user.id))
    await _start_cooldown(redis, str(user.id))
    log.info("verification_code.resent", user_id=str(user.id), email=user.email)
    return str(user.id), code


async def resend_verification_on_login(db: AsyncSession, redis: Redis, email: str) -> None:
    """Re-issue and email a verification code when an unverified account attempts
    to log in with correct credentials.

    Reaching this point means the password already matched (see authenticate_user),
    so only the account owner can trigger it. Honours the resend cooldown, so a
    login loop cannot email-bomb the address, and stays silent either way.
    """
    user = await db.scalar(select(User).where(User.email == email))
    if not user or user.is_verified:
        return
    if await _cooldown_active(redis, str(user.id)):
        return
    code = await issue_verification_code(redis, str(user.id))
    await _start_cooldown(redis, str(user.id))
    from app.tasks.notification_tasks import send_verification_email
    send_verification_email.delay(str(user.id), code, VERIFY_CODE_TTL_MINUTES)
    log.info("verification_code.resent_on_login", user_id=str(user.id), email=user.email)


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User:
    user = await db.scalar(select(User).where(User.email == email))
    if not user or not user.password_hash or not await verify_password(password, user.password_hash):
        raise UnauthorizedError("Invalid credentials")
    if user.is_suspended:
        msg = f"Account suspended: {user.suspension_reason}" if user.suspension_reason else "Account suspended"
        raise UnauthorizedError(msg)
    if not user.is_verified:
        raise EmailNotVerifiedError("Please verify your email before logging in")
    return user


async def is_newsletter_subscriber(db: AsyncSession, email: str) -> bool:
    row = await db.scalar(
        select(NewsletterSubscriber).where(
            NewsletterSubscriber.email == email,
            NewsletterSubscriber.is_active == True,  # noqa: E712
        )
    )
    return row is not None


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
        if user.is_suspended:
            msg = f"Account suspended: {user.suspension_reason}" if user.suspension_reason else "Account suspended"
            raise UnauthorizedError(msg)
        if avatar_url and user.avatar_url != avatar_url:
            user.avatar_url = avatar_url
            await db.commit()
            await db.refresh(user)
        return user

    # Second: find by email and link the OAuth account
    user = await db.scalar(select(User).where(User.email == email))
    if user:
        if user.is_suspended:
            msg = f"Account suspended: {user.suspension_reason}" if user.suspension_reason else "Account suspended"
            raise UnauthorizedError(msg)
        user.oauth_provider = provider
        user.oauth_provider_id = provider_id
        # The provider confirmed this email, so linking it verifies the account.
        user.is_verified = True
        if avatar_url:
            user.avatar_url = avatar_url
        await db.commit()
        await db.refresh(user)
        return user

    # Third: create new OAuth-only user. The provider has already confirmed the
    # email address, so the account is verified from the start.
    user = User(
        email=email,
        full_name=full_name,
        password_hash=None,
        role=UserRole.user,
        avatar_url=avatar_url,
        oauth_provider=provider,
        oauth_provider_id=provider_id,
        is_verified=True,
    )
    db.add(user)
    try:
        await db.commit()
        await db.refresh(user)
        from app.tasks.notification_tasks import (
            send_registration_email, send_new_user_admin_notification,
        )
        send_registration_email.delay(str(user.id))
        send_new_user_admin_notification.delay(str(user.id))
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


async def delete_user(
    db: AsyncSession,
    user: User,
    redis: Redis,
    refresh_token: str | None,
    actor: str = "user",
) -> None:
    """Permanently delete an account and everything that references it.

    ``actor`` is "user" for self-deletion via DELETE /auth/me and "admin" when
    an admin removes the account; it only changes the wording of the internal
    notification, not what gets deleted.
    """
    from app.models.ai_generation import AIGeneration
    from app.models.download import Download
    from app.models.like import Like
    from app.models.purchase import Purchase
    from app.models.subscription import Subscription
    from app.models.iap_subscription import IapSubscription
    from app.models.loop import Loop
    from app.models.stem_pack import StemPack, Stem
    from app.models.drum_kit import DrumKit
    from app.models.drone_pad import DronePadCategory, Drone, DronePad

    if refresh_token:
        await revoke_refresh_token(redis, refresh_token)

    # Read everything the notifications need before the row is deleted.
    user_email = user.email
    user_full_name = user.full_name
    uid = user.id
    user_provider = user.oauth_provider or "email"
    user_joined_at = user.created_at

    # Transactional records owned by the user
    await db.execute(delete(AIGeneration).where(AIGeneration.user_id == uid))
    await db.execute(delete(Download).where(Download.user_id == uid))
    await db.execute(delete(Like).where(Like.user_id == uid))
    await db.execute(delete(Purchase).where(Purchase.user_id == uid))
    await db.execute(delete(Subscription).where(Subscription.user_id == uid))
    # iap_subscriptions has no ON DELETE CASCADE, so a user who ever held a
    # Kiɗa Premium entitlement (store, RevenueCat or admin grant) would
    # otherwise fail the users delete below with a foreign-key violation.
    await db.execute(delete(IapSubscription).where(IapSubscription.user_id == uid))

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

    # Drones carry their own created_by and can live under a category someone
    # else owns, so collect by both routes — filtering on the category alone
    # leaves those drones behind and the users delete then fails on
    # drones_created_by_fkey.
    drone_criteria = Drone.created_by == uid
    if drone_cat_ids:
        drone_criteria = drone_criteria | Drone.category_id.in_(drone_cat_ids)
    drone_ids = (await db.scalars(select(Drone.id).where(drone_criteria))).all()

    if drone_ids:
        drone_pad_ids = (await db.scalars(select(DronePad.id).where(DronePad.drone_id.in_(drone_ids)))).all()
        if drone_pad_ids:
            await db.execute(delete(Download).where(Download.drone_pad_id.in_(drone_pad_ids)))
            await db.execute(delete(Purchase).where(Purchase.drone_pad_id.in_(drone_pad_ids)))
            await db.execute(delete(DronePad).where(DronePad.id.in_(drone_pad_ids)))
        await db.execute(delete(Drone).where(Drone.id.in_(drone_ids)))
    if drone_cat_ids:
        await db.execute(delete(DronePadCategory).where(DronePadCategory.id.in_(drone_cat_ids)))

    await db.execute(delete(User).where(User.id == uid))
    await db.commit()

    # The suspension flag is mirrored in Redis by the admin suspend endpoint and
    # is not covered by the database delete.
    await redis.delete(f"suspended:{uid}")

    from app.services.email_service import send_email, account_deleted_html, account_deleted_text
    await send_email(
        to=user_email,
        subject="Your Kida account has been deleted",
        html=account_deleted_html(user_full_name),
        text=account_deleted_text(user_full_name),
    )

    from app.tasks.notification_tasks import send_account_deleted_admin_notification
    send_account_deleted_admin_notification.delay(
        str(uid),
        user_full_name,
        user_email,
        user_provider,
        user_joined_at.isoformat() if user_joined_at else None,
        actor,
    )
    log.info("account_deleted", user_id=str(uid), email=user_email, actor=actor)


async def get_user_by_id(db: AsyncSession, user_id: str) -> User:
    import uuid as _uuid
    user = await db.get(User, _uuid.UUID(user_id))
    if not user:
        raise UnauthorizedError("User not found")
    return user
