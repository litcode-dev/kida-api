import secrets

import structlog
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from app.database import get_db
from app.middleware.auth_middleware import get_current_user, get_redis
from app.middleware.rate_limit import limiter
from app.services import auth_service
from app.services import deletion_request_service
from app.services import oauth_service
from app.exceptions import AppError, EmailNotVerifiedError
from app.schemas.user import (
    UserRegister, UserLogin, UserResponse, TokenResponse,
    RefreshRequest, OAuthCallbackRequest, GoogleTokenRequest, AppleTokenRequest, DeleteAccountRequest,
    VerifyEmailRequest, ResendVerificationRequest,
    DeletionRequestCreate, DeletionRequestConfirm,
)
from app.schemas.common import success
from app.config import get_settings

log = structlog.get_logger()

router = APIRouter(prefix="/auth", tags=["auth"])

OAUTH_STATE_COOKIE = "oauth_state"


def _verify_oauth_state(request: Request, submitted: str | None) -> None:
    """Check the callback against the state issued by /oauth/google.

    Without this the endpoint accepts any authorization code from anyone, which
    is the CSRF the state parameter exists to prevent: an attacker who gets a
    victim's browser to post their own code has the victim's session signed in
    as the attacker's account. The cookie is httponly, so only the browser that
    started the flow can echo it back.

    Native apps do not go through this route at all — they hold the token flow
    at /oauth/google/token — so requiring the cookie here costs them nothing.
    """
    issued = request.cookies.get(OAUTH_STATE_COOKIE)
    if not issued or not submitted or not secrets.compare_digest(issued, submitted):
        log.warning(
            "oauth_state_mismatch",
            has_cookie=bool(issued),
            has_state=bool(submitted),
        )
        raise AppError(
            "This sign-in link has expired or is not valid. Start again from the "
            "sign-in page.",
            status_code=400,
        )


@router.post("/register")
@limiter.limit("10/minute")
async def register(
    request: Request,
    body: UserRegister,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    user, code = await auth_service.register_user(db, redis, body.email, body.password, body.full_name)
    # The account is committed by now. If the broker is down, saying so would
    # send the client to retry a registration that would then collide on the
    # email — /auth/resend-verification is the recovery path instead.
    try:
        from app.tasks.notification_tasks import send_verification_email
        send_verification_email.delay(str(user.id), code, auth_service.VERIFY_CODE_TTL_MINUTES)
    except Exception as exc:  # noqa: BLE001 - the account is already created
        log.error("verification_email.enqueue_failed", user_id=str(user.id), error=str(exc))
    return success(
        UserResponse.model_validate(user).model_dump(),
        "Registration successful. Check your email for a verification code.",
    )


@router.post("/verify-email")
@limiter.limit("10/minute")
async def verify_email(
    request: Request,
    body: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    user = await auth_service.verify_email(db, redis, body.email, body.code)
    access_token = auth_service.create_access_token(str(user.id), user.role.value)
    refresh_token = auth_service.create_refresh_token()
    subscribed = await auth_service.is_newsletter_subscriber(db, user.email)
    await auth_service.store_refresh_token(redis, refresh_token, str(user.id))
    return success(
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            full_name=user.full_name,
            role=user.role,
            avatar_url=user.avatar_url,
            subscribed_to_newsletter=subscribed,
        ).model_dump(),
        "Email verified",
    )


@router.post("/resend-verification")
@limiter.limit("5/minute")
async def resend_verification(
    request: Request,
    body: ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    result = await auth_service.resend_verification_code(db, redis, body.email)
    if result is not None:
        user_id, code = result
        from app.tasks.notification_tasks import send_verification_email
        send_verification_email.delay(user_id, code, auth_service.VERIFY_CODE_TTL_MINUTES)
    return success(message="If your email is registered and unverified, a new verification code has been sent.")


@router.post("/login")
@limiter.limit("20/minute")
async def login(
    request: Request,
    body: UserLogin,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    try:
        user = await auth_service.authenticate_user(db, body.email, body.password)
    except EmailNotVerifiedError:
        # Credentials were correct but the email is unverified — email a fresh
        # code (subject to the resend cooldown) before surfacing the 403.
        await auth_service.resend_verification_on_login(db, redis, body.email)
        raise
    access_token = auth_service.create_access_token(str(user.id), user.role.value)
    refresh_token = auth_service.create_refresh_token()
    subscribed = await auth_service.is_newsletter_subscriber(db, user.email)
    await auth_service.store_refresh_token(redis, refresh_token, str(user.id))
    return success(
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            full_name=user.full_name,
            role=user.role,
            avatar_url=user.avatar_url,
            subscribed_to_newsletter=subscribed,
        ).model_dump(),
        "Login successful",
    )


@router.post("/refresh")
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    user_id = await auth_service.validate_refresh_token(redis, body.refresh_token)
    user = await auth_service.get_user_by_id(db, user_id)
    if user.is_suspended:
        from app.exceptions import UnauthorizedError
        raise UnauthorizedError("Account suspended")
    await auth_service.revoke_refresh_token(redis, body.refresh_token)
    new_refresh = auth_service.create_refresh_token()
    await auth_service.store_refresh_token(redis, new_refresh, user_id)
    access_token = auth_service.create_access_token(user_id, user.role.value)
    return success(
        TokenResponse(access_token=access_token, refresh_token=new_refresh).model_dump(),
        "Token refreshed",
    )


@router.post("/logout")
async def logout(
    body: RefreshRequest,
    redis: Redis = Depends(get_redis),
):
    await auth_service.revoke_refresh_token(redis, body.refresh_token)
    return success(message="Logged out")


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return success(UserResponse.model_validate(user).model_dump())


@router.delete(
    "/me",
    summary="Delete account",
    description=(
        "Permanently deletes the authenticated user's account and everything "
        "attached to it — profile, purchases, subscriptions, likes, downloads, "
        "uploaded content and its stored files — along with the records held by "
        "RevenueCat and OneSignal.\n\n"
        "There is no grace period and no way to undo this. Every session ends "
        "immediately; pass `refresh_token` in the body to revoke the current "
        "session's token too, though every session is revoked regardless. Once "
        "it returns, the email address is free to register again as a new "
        "account.\n\n"
        "A repeat call from the same token gets 401, because the account it "
        "authenticates no longer exists."
    ),
    responses={401: {"description": "Missing or invalid token"}},
)
async def delete_account(
    body: DeleteAccountRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    user=Depends(get_current_user),
):
    await auth_service.delete_user(db, user, redis, body.refresh_token)
    return success(message="Account deleted")


@router.post(
    "/deletion-request",
    summary="Request account deletion without signing in",
    description=(
        "For the public account-deletion page: starts deletion for people who "
        "cannot sign in (lost password, an OAuth account they no longer control, "
        "a device they no longer have).\n\n"
        "If the address belongs to an account, a single-use confirmation link is "
        "emailed to it. Nothing is deleted by this call — control of the inbox has "
        "to be demonstrated first.\n\n"
        "Always returns the same response whether or not the address is "
        "registered, so this cannot be used to test who has an account."
    ),
)
@limiter.limit("3/hour")
async def request_deletion(
    request: Request,
    body: DeletionRequestCreate,
    db: AsyncSession = Depends(get_db),
):
    await deletion_request_service.start_request(db, body.email)
    # Deliberately unconditional. The one thing this endpoint must never do is
    # answer "is this address registered?" — so it says the same either way.
    return success(
        message=(
            "If that address has a Kida account, we've emailed a link to confirm "
            "the deletion. The link expires shortly."
        )
    )


@router.post(
    "/deletion-request/confirm",
    summary="Confirm a deletion requested by email",
    description=(
        "Redeems the single-use link from `/auth/deletion-request` and deletes "
        "the account on the same terms as in-app deletion: everything is erased "
        "at once, with no grace period and no way to undo it.\n\n"
        "A POST, not a GET, precisely so that a mail client or link scanner "
        "prefetching the URL cannot delete an account nobody confirmed.\n\n"
        "Returns the same response for a token that is expired, already used, "
        "forged, or belongs to no account."
    ),
)
@limiter.limit("10/hour")
async def confirm_deletion_request(
    request: Request,
    body: DeletionRequestConfirm,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await deletion_request_service.confirm_request(db, redis, body.token)
    return success(
        message=(
            "If that link was valid, the account has been deleted and a "
            "confirmation email is on its way."
        )
    )


@router.get("/oauth/google")
@limiter.limit("20/minute")
async def google_oauth_redirect(request: Request):
    """Redirect the browser directly to Google's OAuth2 authorization page."""
    from fastapi.responses import RedirectResponse
    state = secrets.token_urlsafe(16)
    url = oauth_service.get_google_auth_url(state)
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=state,
        httponly=True,
        max_age=300,  # 5 minutes
        samesite="lax",
        # Only over TLS wherever the flow itself runs over TLS; a local http
        # redirect_uri would never receive a secure cookie back.
        secure=get_settings().google_redirect_uri.startswith("https://"),
    )
    return response


@router.post("/oauth/google/token")
@limiter.limit("20/minute")
async def google_oauth_mobile(
    request: Request,
    body: GoogleTokenRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Exchange a Google access_token (from mobile SDK) for LitMusic JWT tokens."""
    user_info = await oauth_service.get_google_user_info(body.access_token)
    user = await auth_service.find_or_create_oauth_user(
        db,
        email=user_info["email"],
        full_name=user_info.get("name", ""),
        provider="google",
        provider_id=user_info["sub"],
        avatar_url=user_info.get("picture"),
    )
    access_token = auth_service.create_access_token(str(user.id), user.role.value)
    refresh_token = auth_service.create_refresh_token()
    subscribed = await auth_service.is_newsletter_subscriber(db, user.email)
    await auth_service.store_refresh_token(redis, refresh_token, str(user.id))
    return success(
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            full_name=user.full_name,
            role=user.role,
            avatar_url=user.avatar_url,
            subscribed_to_newsletter=subscribed,
        ).model_dump(),
        "OAuth login successful",
    )


@router.post("/oauth/apple/token")
@limiter.limit("20/minute")
async def apple_oauth_mobile(
    request: Request,
    body: AppleTokenRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Exchange an Apple identity_token (from iOS Sign In with Apple) for LitMusic JWT tokens."""
    claims = await oauth_service.verify_apple_identity_token(body.identity_token)
    oauth_service.reject_unverified_provider_email("apple", claims.get("email_verified"))
    # Guaranteed present: verify_apple_identity_token requires the claim.
    provider_id = claims["sub"]
    email = claims.get("email") or body.email
    if not email:
        raise AppError("Email not provided by Apple and not included in request", status_code=422)
    full_name = body.full_name or email.split("@")[0]
    user = await auth_service.find_or_create_oauth_user(
        db,
        email=email,
        full_name=full_name,
        provider="apple",
        provider_id=provider_id,
    )
    access_token = auth_service.create_access_token(str(user.id), user.role.value)
    refresh_token = auth_service.create_refresh_token()
    subscribed = await auth_service.is_newsletter_subscriber(db, user.email)
    await auth_service.store_refresh_token(redis, refresh_token, str(user.id))
    return success(
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            full_name=user.full_name,
            role=user.role,
            avatar_url=user.avatar_url,
            subscribed_to_newsletter=subscribed,
        ).model_dump(),
        "OAuth login successful",
    )


@router.post("/oauth/google/callback")
@limiter.limit("20/minute")
async def google_oauth_callback(
    request: Request,
    response: Response,
    body: OAuthCallbackRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Exchange a Google authorization code for LitMusic JWT tokens."""
    _verify_oauth_state(request, body.state)
    # One-time use: a state that has been redeemed must not validate a second
    # authorization code.
    response.delete_cookie(OAUTH_STATE_COOKIE)
    token_data = await oauth_service.exchange_google_code(body.code)
    user_info = await oauth_service.get_google_user_info(token_data["access_token"])
    user = await auth_service.find_or_create_oauth_user(
        db,
        email=user_info["email"],
        full_name=user_info.get("name", ""),
        provider="google",
        provider_id=user_info["sub"],
        avatar_url=user_info.get("picture"),
    )
    access_token = auth_service.create_access_token(str(user.id), user.role.value)
    refresh_token = auth_service.create_refresh_token()
    subscribed = await auth_service.is_newsletter_subscriber(db, user.email)
    await auth_service.store_refresh_token(redis, refresh_token, str(user.id))
    return success(
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            full_name=user.full_name,
            role=user.role,
            avatar_url=user.avatar_url,
            subscribed_to_newsletter=subscribed,
        ).model_dump(),
        "OAuth login successful",
    )
