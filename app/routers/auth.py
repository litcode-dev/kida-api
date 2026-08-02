import secrets
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from app.database import get_db
from app.middleware.auth_middleware import get_current_user, get_redis
from app.middleware.rate_limit import limiter
from app.services import auth_service
from app.services import oauth_service
from app.exceptions import AppError
from app.schemas.user import (
    UserRegister, UserLogin, UserResponse, TokenResponse,
    RefreshRequest, OAuthCallbackRequest, GoogleTokenRequest, AppleTokenRequest, DeleteAccountRequest,
    VerifyEmailRequest, ResendVerificationRequest,
)
from app.schemas.common import success

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register")
@limiter.limit("10/minute")
async def register(
    request: Request,
    body: UserRegister,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    user, code = await auth_service.register_user(db, redis, body.email, body.password, body.full_name)
    from app.tasks.notification_tasks import send_verification_email
    send_verification_email.delay(str(user.id), code, auth_service.VERIFY_CODE_TTL_MINUTES)
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
    user = await auth_service.authenticate_user(db, body.email, body.password)
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
        "Permanently deletes the authenticated user's account and all associated data. "
        "Pass `refresh_token` in the body to also revoke the current session. "
        "This action is irreversible."
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


@router.get("/oauth/google")
async def google_oauth_redirect():
    """Redirect the browser directly to Google's OAuth2 authorization page."""
    from fastapi.responses import RedirectResponse
    state = secrets.token_urlsafe(16)
    url = oauth_service.get_google_auth_url(state)
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        max_age=300,  # 5 minutes
        samesite="lax",
    )
    return response


@router.post("/oauth/google/token")
async def google_oauth_mobile(
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
async def apple_oauth_mobile(
    body: AppleTokenRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Exchange an Apple identity_token (from iOS Sign In with Apple) for LitMusic JWT tokens."""
    claims = await oauth_service.verify_apple_identity_token(body.identity_token)
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
async def google_oauth_callback(
    body: OAuthCallbackRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Exchange a Google authorization code for LitMusic JWT tokens."""
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
