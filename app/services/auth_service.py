import asyncio
import secrets
import uuid
from datetime import datetime, timedelta, timezone
import jwt
import bcrypt
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func
from sqlalchemy.exc import IntegrityError
from app.config import get_settings
from app.models.user import User, UserRole
from app.models.newsletter import NewsletterSubscriber
from app.exceptions import UnauthorizedError, ConflictError, EmailNotVerifiedError, AppError

settings = get_settings()
log = structlog.get_logger()

ALGORITHM = "HS256"
REFRESH_PREFIX = "refresh:"
# Set of a user's live refresh tokens, so every session can be revoked at once.
REFRESH_INDEX_PREFIX = "refresh_user:"
VERIFY_PREFIX = "email_verify:"
VERIFY_COOLDOWN_PREFIX = "email_verify_cooldown:"
VERIFY_CODE_TTL_MINUTES = 15
VERIFY_RESEND_COOLDOWN_SECONDS = 60


def _deletion_actors() -> dict:
    """``delete_user``'s ``actor`` string → the audit enum. Imported lazily to
    keep this module free of a model import cycle."""
    from app.models.deletion_audit import DeletionActor
    return {
        "user": DeletionActor.user,
        "admin": DeletionActor.admin,
        "email_request": DeletionActor.email_request,
    }


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


def _as_str(value) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else value


async def store_refresh_token(redis: Redis, token: str, user_id: str) -> None:
    """Store a refresh token, and index it against its owner.

    The index is what makes "sign out everywhere" possible. Without it a token
    can only be reached by presenting it, so deleting an account could revoke
    the one session that asked and leave every other device's token sitting in
    Redis until it aged out.
    """
    key = f"{REFRESH_PREFIX}{token}"
    expire_seconds = settings.refresh_token_expire_days * 86400
    await redis.setex(key, expire_seconds, user_id)

    index = f"{REFRESH_INDEX_PREFIX}{user_id}"
    await redis.sadd(index, token)
    # Bump the index's own TTL each time so it outlives its newest member and
    # cannot linger forever once the account stops being used.
    await redis.expire(index, expire_seconds)


async def validate_refresh_token(redis: Redis, token: str) -> str:
    key = f"{REFRESH_PREFIX}{token}"
    user_id = await redis.get(key)
    if not user_id:
        raise UnauthorizedError("Refresh token invalid or expired")
    return _as_str(user_id)


async def revoke_refresh_token(redis: Redis, token: str) -> None:
    key = f"{REFRESH_PREFIX}{token}"
    user_id = _as_str(await redis.get(key))
    await redis.delete(key)
    if user_id:
        await redis.srem(f"{REFRESH_INDEX_PREFIX}{user_id}", token)


async def revoke_all_refresh_tokens(redis: Redis, user_id: str) -> int:
    """Revoke every session an account holds. Returns how many were removed.

    Best effort: a Redis failure here must not stop an account deletion, since
    the tokens are unusable the moment the account row is gone (``/auth/refresh``
    reloads the user and rejects it). This is about not leaving the identifier
    lying around, not about access control.
    """
    index = f"{REFRESH_INDEX_PREFIX}{user_id}"
    try:
        tokens = [_as_str(t) for t in await redis.smembers(index)]
        if tokens:
            await redis.delete(*[f"{REFRESH_PREFIX}{t}" for t in tokens if t])
        await redis.delete(index)
        return len(tokens)
    except Exception as exc:  # noqa: BLE001 - never block a deletion on Redis
        log.error("refresh_token_revoke_all_failed", user_id=user_id, error=str(exc))
        return 0


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


async def _loop_s3_keys(db: AsyncSession, loop_ids) -> list[str]:
    from app.models.loop import Loop
    from app.services import s3_service

    rows = await db.execute(
        select(Loop.id, Loop.file_s3_key, Loop.preview_s3_key, Loop.thumbnail_s3_key)
        .where(Loop.id.in_(loop_ids))
    )
    keys: list[str] = []
    for loop_id, *stored in rows:
        keys += [k for k in stored if k]
        # The raw upload is deleted by the processing job on success, but a loop
        # stuck mid-processing still has one.
        keys.append(s3_service.s3_key_for_raw_loop(str(loop_id)))
    return keys


async def _stem_s3_keys(db: AsyncSession, stem_pack_ids) -> list[str]:
    from app.models.stem_pack import Stem, StemArrangement, StemArrangementTrack
    from app.services import s3_service

    rows = await db.execute(
        select(Stem.id, Stem.file_s3_key, Stem.preview_s3_key)
        .where(Stem.stem_pack_id.in_(stem_pack_ids))
    )
    keys: list[str] = []
    for stem_id, *stored in rows:
        keys += [k for k in stored if k]
        # The raw upload is deleted by the processing job on success, but a stem
        # stuck mid-processing still has one.
        keys.append(s3_service.s3_key_for_raw_stem(str(stem_id)))

    # Rendered arrangements are separate objects again — one per instrument.
    track_rows = await db.execute(
        select(StemArrangementTrack.file_s3_key, StemArrangementTrack.preview_s3_key)
        .join(StemArrangement, StemArrangementTrack.arrangement_id == StemArrangement.id)
        .where(StemArrangement.stem_pack_id.in_(stem_pack_ids))
    )
    keys += [k for row in track_rows for k in row if k]
    return keys


async def _drum_kit_s3_keys(db: AsyncSession, drum_kit_ids) -> list[str]:
    from app.models.drum_kit import DrumKit, DrumSample
    from app.services import s3_service

    keys = [
        k for k in await db.scalars(
            select(DrumKit.thumbnail_s3_key).where(DrumKit.id.in_(drum_kit_ids))
        ) if k
    ]
    rows = await db.execute(
        select(DrumSample.id, DrumSample.file_s3_key, DrumSample.preview_s3_key)
        .where(DrumSample.drum_kit_id.in_(drum_kit_ids))
    )
    for sample_id, *stored in rows:
        keys += [k for k in stored if k]
        keys.append(s3_service.s3_key_for_raw_drum_sample(str(sample_id)))
    return keys


async def _drone_s3_keys(db: AsyncSession, drone_ids) -> list[str]:
    from app.models.drone_pad import DronePad
    from app.services import s3_service

    rows = await db.execute(
        select(DronePad.file_s3_key, DronePad.preview_s3_key, DronePad.thumbnail_s3_key)
        .where(DronePad.drone_id.in_(drone_ids))
    )
    keys = [k for row in rows for k in row if k]
    keys += [s3_service.s3_key_for_raw_drone(str(drone_id)) for drone_id in drone_ids]
    return keys


async def _delete_s3_objects(keys: list[str], user_id: str) -> None:
    """Best-effort removal of a purged user's files.

    Runs after the database transaction has committed, so a storage failure
    leaves an orphaned object rather than a half-deleted account. Failures are
    logged with the key so they can be swept up later.
    """
    from app.services import s3_service

    for key in dict.fromkeys(k for k in keys if k):  # dedupe, keep order
        try:
            await s3_service.delete_object(key)
        except Exception as exc:  # noqa: BLE001 - one object must not stop the rest
            log.error("account_purge.s3_delete_failed", user_id=user_id, key=key, error=str(exc))


async def delete_user(
    db: AsyncSession,
    user: User,
    redis: Redis,
    refresh_token: str | None,
    actor: str = "user",
) -> None:
    """Permanently delete an account and everything that references it.

    There is no pending state and nothing to undo: by the time this returns the
    rows are gone. ``actor`` is "user" for self-deletion via DELETE /auth/me,
    "email_request" for the confirmed public deletion link, and "admin" when an
    admin removes the account; it changes the wording of the internal
    notification and what the audit records, not what gets deleted.
    """
    from app.models.ai_generation import AIGeneration
    from app.models.download import Download
    from app.models.like import Like
    from app.models.purchase import Purchase
    from app.models.subscription import Subscription
    from app.models.iap_subscription import IapSubscription
    from app.models.loop import Loop
    from app.models.stem_pack import (
        StemPack, Stem, StemPart, StemArrangement, StemArrangementItem, StemArrangementTrack,
    )
    from app.models.drum_kit import DrumKit, DrumSample
    from app.models.drone_pad import DronePadCategory, Drone, DronePad
    from app.models.app_download_request import AppDownloadRequest

    if refresh_token:
        await revoke_refresh_token(redis, refresh_token)

    # Read everything the notifications need before the row is deleted.
    user_email = user.email
    user_full_name = user.full_name
    uid = user.id
    user_provider = user.oauth_provider or "email"
    user_joined_at = user.created_at
    # The account holder asked for this and it is being carried out in the same
    # breath, so the request time is now. An admin removal has no such request.
    requested_at = None if actor == "admin" else datetime.now(timezone.utc)

    # Identifiers the third parties know this person by, read while the rows
    # still exist. Handed to the propagation queue below.
    stored_rc_ids = (await db.scalars(
        select(IapSubscription.app_user_id).where(IapSubscription.user_id == uid)
    )).all()
    # Whatever RevenueCat actually recorded, plus both ids we have ever sent it:
    # the backend reconciles with the user UUID, the client has been reporting
    # the lowercased email. Deleting under all of them is the only way to be
    # sure the subscriber is gone. See docs/account-deletion.md.
    revenuecat_ids = [i for i in stored_rc_ids if i] + [
        str(uid), user_email.strip().lower()
    ]
    onesignal_subscription_id = user.onesignal_player_id

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

    # The address itself is personal data and lives outside the users table too.
    # Both are keyed on the email rather than a user id, and neither is matched
    # case-sensitively when it is written, so compare lowercased.
    await db.execute(
        delete(NewsletterSubscriber).where(
            func.lower(NewsletterSubscriber.email) == user_email.lower()
        )
    )
    await db.execute(
        delete(AppDownloadRequest).where(
            func.lower(AppDownloadRequest.email) == user_email.lower()
        )
    )

    # S3 objects are collected while the rows still exist and deleted after the
    # transaction commits — an object delete cannot be rolled back, so it must
    # not run before the database says the account is really gone.
    s3_keys: list[str] = []

    # Producer content created by the user — delete children before parents
    loop_ids = (await db.scalars(select(Loop.id).where(Loop.created_by == uid))).all()
    if loop_ids:
        s3_keys += await _loop_s3_keys(db, loop_ids)
        await db.execute(delete(Download).where(Download.loop_id.in_(loop_ids)))
        await db.execute(delete(Like).where(Like.loop_id.in_(loop_ids)))
        await db.execute(delete(Purchase).where(Purchase.loop_id.in_(loop_ids)))
        await db.execute(delete(Loop).where(Loop.id.in_(loop_ids)))

    stem_pack_ids = (await db.scalars(select(StemPack.id).where(StemPack.created_by == uid))).all()
    if stem_pack_ids:
        s3_keys += await _stem_s3_keys(db, stem_pack_ids)
        stem_ids = (await db.scalars(
            select(Stem.id).where(Stem.stem_pack_id.in_(stem_pack_ids))
        )).all()
        if stem_ids:
            await db.execute(delete(Download).where(Download.stem_id.in_(stem_ids)))
        arrangement_ids = (await db.scalars(
            select(StemArrangement.id).where(StemArrangement.stem_pack_id.in_(stem_pack_ids))
        )).all()
        if arrangement_ids:
            await db.execute(
                delete(StemArrangementTrack)
                .where(StemArrangementTrack.arrangement_id.in_(arrangement_ids))
            )
            await db.execute(
                delete(StemArrangementItem)
                .where(StemArrangementItem.arrangement_id.in_(arrangement_ids))
            )
            await db.execute(delete(StemArrangement).where(StemArrangement.id.in_(arrangement_ids)))
        await db.execute(delete(Stem).where(Stem.stem_pack_id.in_(stem_pack_ids)))
        await db.execute(delete(StemPart).where(StemPart.stem_pack_id.in_(stem_pack_ids)))
        await db.execute(delete(Purchase).where(Purchase.stem_pack_id.in_(stem_pack_ids)))
        await db.execute(delete(Like).where(Like.stem_pack_id.in_(stem_pack_ids)))
        await db.execute(delete(StemPack).where(StemPack.id.in_(stem_pack_ids)))

    # Arrangements created by this user under someone else's pack carry their
    # own created_by, so they would otherwise block the users delete.
    orphan_arrangements = (await db.scalars(
        select(StemArrangement.id).where(StemArrangement.created_by == uid)
    )).all()
    if orphan_arrangements:
        track_rows = await db.execute(
            select(StemArrangementTrack.file_s3_key, StemArrangementTrack.preview_s3_key)
            .where(StemArrangementTrack.arrangement_id.in_(orphan_arrangements))
        )
        s3_keys += [k for row in track_rows for k in row if k]
        await db.execute(
            delete(StemArrangementTrack)
            .where(StemArrangementTrack.arrangement_id.in_(orphan_arrangements))
        )
        await db.execute(
            delete(StemArrangementItem)
            .where(StemArrangementItem.arrangement_id.in_(orphan_arrangements))
        )
        await db.execute(delete(StemArrangement).where(StemArrangement.id.in_(orphan_arrangements)))

    drum_kit_ids = (await db.scalars(select(DrumKit.id).where(DrumKit.created_by == uid))).all()
    if drum_kit_ids:
        s3_keys += await _drum_kit_s3_keys(db, drum_kit_ids)
        await db.execute(delete(DrumSample).where(DrumSample.drum_kit_id.in_(drum_kit_ids)))
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
        s3_keys += await _drone_s3_keys(db, drone_ids)
        drone_pad_ids = (await db.scalars(select(DronePad.id).where(DronePad.drone_id.in_(drone_ids)))).all()
        if drone_pad_ids:
            await db.execute(delete(Download).where(Download.drone_pad_id.in_(drone_pad_ids)))
            await db.execute(delete(Purchase).where(Purchase.drone_pad_id.in_(drone_pad_ids)))
            await db.execute(delete(DronePad).where(DronePad.id.in_(drone_pad_ids)))
        await db.execute(delete(Drone).where(Drone.id.in_(drone_ids)))
    if drone_cat_ids:
        await db.execute(delete(DronePadCategory).where(DronePadCategory.id.in_(drone_cat_ids)))

    # The audit row is written in the same transaction as the delete, so a
    # rollback cannot leave a record claiming an account was removed when it
    # wasn't — and the third-party work is queued atomically with it.
    from app.models.deletion_audit import DeletionActor
    from app.services import account_deletion_service

    audit_id = await account_deletion_service.record_deletion(
        db,
        user_id=uid,
        actor=_deletion_actors().get(actor, DeletionActor.user),
        requested_at=requested_at,
        revenuecat_app_user_ids=revenuecat_ids,
        onesignal_subscription_id=onesignal_subscription_id,
        onesignal_external_id=str(uid),
    )

    await db.execute(delete(User).where(User.id == uid))
    await db.commit()

    # The suspension flag is mirrored in Redis by the admin suspend endpoint and
    # is not covered by the database delete.
    await redis.delete(f"suspended:{uid}")
    # Sessions on every other device. Unusable already — the account row is
    # gone — but the tokens should not sit in Redis holding a dead user id.
    await revoke_all_refresh_tokens(redis, str(uid))

    # Only now that the rows are really gone.
    await _delete_s3_objects(s3_keys, str(uid))

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

    # Tell RevenueCat and OneSignal. Queued rather than awaited: the local
    # delete is already committed and must not be held up — or undone — by a
    # third-party outage. The queued row is retried by the beat sweep if this
    # never runs.
    try:
        from app.tasks.deletion_tasks import propagate_account_deletion
        propagate_account_deletion.delay(str(audit_id))
    except Exception as exc:  # noqa: BLE001 - the sweep will pick it up
        log.error(
            "account_deletion.propagation_enqueue_failed",
            audit_id=str(audit_id), error=str(exc),
        )

    log.info("account_deleted", user_id=str(uid), actor=actor, audit_id=str(audit_id))


async def get_user_by_id(db: AsyncSession, user_id: str) -> User:
    import uuid as _uuid
    user = await db.get(User, _uuid.UUID(user_id))
    if not user:
        raise UnauthorizedError("User not found")
    return user
