import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import AppError, ConflictError, NotFoundError
from app.models.app_download_request import (
    AppDownloadRequest, STATUS_FULFILLED, STATUS_PENDING_PAYMENT,
)
from app.models.desktop_app import DesktopApp
from app.models.purchase import PaymentProvider
from app.schemas.app_download import FREE_PLATFORMS, DesktopAppCreate, DesktopAppUpdate
from app.services import payments, s3_service
from app.services.payments import VerifiedTransaction

log = structlog.get_logger()

LINK_TTL_DAYS = 3
PRESIGN_TTL_SECONDS = 300
# Mobile links are shown in the public app list, which a client may hold on
# to for a while, so they outlive the redirect's 5 minutes.
PUBLIC_LINK_TTL_SECONDS = 3600
RESEND_COOLDOWN_MINUTES = 15

OS_LABELS = {
    "macos": "macOS", "windows": "Windows", "linux": "Linux",
    "android": "Android", "ios": "iOS",
}
DEFAULT_APP_NAME = "Kida"

# Metadata key on a checkout that marks it as an app download, so the shared
# payment webhook knows to hand it here rather than record a Purchase.
CHECKOUT_METADATA_KEY = "app_download_request_id"

# Which gateway to use for a currency when the caller does not name one: the
# first of these this deployment has credentials for.
_PROVIDER_PREFERENCE = {
    "NGN": (PaymentProvider.paystack, PaymentProvider.flutterwave,
            PaymentProvider.squad, PaymentProvider.stripe),
    "USD": (PaymentProvider.stripe, PaymentProvider.flutterwave, PaymentProvider.paystack),
}


@dataclass(frozen=True)
class DownloadRequestResult:
    """What the public endpoint needs to answer with.

    ``request`` is None when nothing was created (a throttled free request);
    ``checkout_url`` is set only when the app is paid.
    """

    request: AppDownloadRequest | None
    app: DesktopApp | None = None
    checkout_url: str | None = None


# -- admin: managing published apps ------------------------------------------

async def _ensure_unique(
    db: AsyncSession, name: str, exclude_id: uuid.UUID | None = None
) -> None:
    query = select(DesktopApp.id).where(func.lower(DesktopApp.name) == name.lower())
    if exclude_id is not None:
        query = query.where(DesktopApp.id != exclude_id)
    if await db.scalar(query.limit(1)) is not None:
        raise ConflictError(f"An app named '{name}' already exists")


async def create_app(db: AsyncSession, data: DesktopAppCreate) -> DesktopApp:
    await _ensure_unique(db, data.name)
    # New apps go to the end of the list.
    last = await db.scalar(select(func.max(DesktopApp.position)))
    app = DesktopApp(**data.model_dump(), position=0 if last is None else last + 1)
    db.add(app)
    await db.commit()
    await db.refresh(app)
    return app


async def list_apps(db: AsyncSession, active_only: bool = False) -> list[DesktopApp]:
    query = select(DesktopApp).order_by(DesktopApp.position, func.lower(DesktopApp.name))
    if active_only:
        query = query.where(DesktopApp.is_active.is_(True))
    return list((await db.scalars(query)).all())


async def get_app(db: AsyncSession, app_id: uuid.UUID) -> DesktopApp:
    app = await db.get(DesktopApp, app_id)
    if app is None:
        raise NotFoundError("App not found")
    return app


async def update_app(db: AsyncSession, app_id: uuid.UUID, data: DesktopAppUpdate) -> DesktopApp:
    app = await get_app(db, app_id)
    changes = data.model_dump(exclude_unset=True)

    platforms = dict(app.platforms or {})
    for os, url in (changes.pop("platforms", None) or {}).items():
        if url is None:
            platforms.pop(os, None)
        else:
            platforms[os] = url

    # Validate the merged result, so e.g. flipping is_paid on an app that
    # never had a price is refused rather than saved half-configured.
    merged = {
        "name": app.name, "description": app.description,
        "platforms": platforms, "is_paid": app.is_paid,
        "price": app.price, "currency": app.currency, "is_active": app.is_active,
        **changes,
    }
    try:
        validated = DesktopAppCreate(**merged)
    except ValueError as exc:
        raise AppError(_first_error(exc), status_code=422)

    if validated.name.lower() != app.name.lower():
        await _ensure_unique(db, validated.name, exclude_id=app.id)

    for field, value in validated.model_dump().items():
        setattr(app, field, value)
    await db.commit()
    await db.refresh(app)
    return app


async def reorder_apps(db: AsyncSession, app_ids: list[uuid.UUID]) -> list[DesktopApp]:
    """Show the given apps first, in this order; the rest follow as they were."""
    apps = await list_apps(db)
    by_id = {app.id: app for app in apps}
    unknown = [str(app_id) for app_id in app_ids if app_id not in by_id]
    if unknown:
        raise NotFoundError(f"Unknown app id(s): {', '.join(unknown)}")

    listed = set(app_ids)
    ordered = [by_id[app_id] for app_id in app_ids] + [a for a in apps if a.id not in listed]
    for position, app in enumerate(ordered):
        app.position = position
    await db.commit()
    return await list_apps(db)


async def delete_app(db: AsyncSession, app_id: uuid.UUID) -> None:
    app = await get_app(db, app_id)
    await db.delete(app)
    await db.commit()


def _first_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return str(errors()[0]["msg"]).removeprefix("Value error, ")
        except (IndexError, KeyError, TypeError):
            pass
    return str(exc)


# -- public: requesting a download --------------------------------------------

async def _find_app(db: AsyncSession, name: str, os: str) -> DesktopApp:
    app = await db.scalar(
        select(DesktopApp).where(
            func.lower(DesktopApp.name) == name.lower(),
            DesktopApp.is_active.is_(True),
        )
    )
    if app is None:
        raise NotFoundError(f"'{name}' is not available")
    if os not in (app.platforms or {}):
        raise NotFoundError(f"'{app.name}' is not available for {OS_LABELS.get(os, os)}")
    return app


async def _recently_emailed(db: AsyncSession, email: str) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=RESEND_COOLDOWN_MINUTES)
    recent = await db.scalar(
        select(AppDownloadRequest.id)
        .where(
            AppDownloadRequest.email == email,
            AppDownloadRequest.status == STATUS_FULFILLED,
            AppDownloadRequest.created_at >= cutoff,
        )
        .limit(1)
    )
    return recent is not None


async def create_request(db: AsyncSession, email: str, os: str) -> AppDownloadRequest | None:
    """Persist a free download request for the default installer.

    Returns None (creating nothing) when this email already requested a link
    within the last RESEND_COOLDOWN_MINUTES — this throttles per-recipient email
    so the public endpoint cannot be used to email-bomb a victim.
    """
    result = await request_download(db, email, os)
    return result.request


async def request_download(
    db: AsyncSession,
    email: str,
    os: str,
    app_name: str | None = None,
    provider: PaymentProvider | None = None,
) -> DownloadRequestResult:
    """Start a download request.

    A free app, an Android or iOS build (always free), or the default installer
    gets a 3-day link straight away, subject to the per-email cooldown. A paid
    app's desktop build gets a pending request and a checkout URL; the link is
    only issued once the payment webhook confirms it.
    """
    if app_name:
        app = await _find_app(db, app_name, os)
    else:
        app = None
        _installer_key(os)  # refuse now rather than email a link that cannot work

    if app is None or not app.is_paid or os in FREE_PLATFORMS:
        if await _recently_emailed(db, email):
            return DownloadRequestResult(request=None, app=app)
        req = AppDownloadRequest(
            email=email,
            os=os,
            token=secrets.token_urlsafe(32),
            expires_at=datetime.now(timezone.utc) + timedelta(days=LINK_TTL_DAYS),
            app_id=app.id if app else None,
            app_name=app.name if app else None,
            status=STATUS_FULFILLED,
        )
        db.add(req)
        await db.commit()
        await db.refresh(req)
        return DownloadRequestResult(request=req, app=app)

    gateway = payments.get_gateway(provider or _default_provider(app.currency))
    gateway.require_configured()

    req = AppDownloadRequest(
        email=email,
        os=os,
        token=secrets.token_urlsafe(32),
        expires_at=None,
        app_id=app.id,
        app_name=app.name,
        status=STATUS_PENDING_PAYMENT,
        amount=app.price,
        currency=app.currency,
        payment_provider=gateway.provider.value,
    )
    db.add(req)
    await db.flush()

    session = await gateway.create_checkout(
        amount=app.price,
        currency=app.currency,
        reference=str(uuid.uuid4()),
        email=email,
        description=f"{app.name} for {OS_LABELS.get(os, os)}",
        metadata={CHECKOUT_METADATA_KEY: str(req.id), "customer_name": email},
    )
    req.payment_reference = session.reference
    await db.commit()
    await db.refresh(req)
    return DownloadRequestResult(request=req, app=app, checkout_url=session.checkout_url)


def _default_provider(currency: str) -> PaymentProvider:
    for provider in _PROVIDER_PREFERENCE.get(currency, ()):
        if payments.get_gateway(provider).is_configured:
            return provider
    raise AppError(f"No payment gateway is configured for {currency}", status_code=503)


# -- payment confirmation ------------------------------------------------------

async def find_paid_request_id(
    db: AsyncSession, verified: VerifiedTransaction, references: list[str | None]
) -> uuid.UUID | None:
    """Which download request a verified payment is for, if any.

    The checkout metadata names it, but not every gateway echoes metadata back
    from its verify endpoint (Squad's does not), so the payment reference saved
    when checkout was created is matched too. ``references`` are the ones the
    webhook carried; each was confirmed by the verify call that produced
    ``verified``.
    """
    raw_id = verified.metadata.get(CHECKOUT_METADATA_KEY)
    if raw_id:
        try:
            return uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            log.warning("app_download_payment.bad_metadata", reference=verified.reference)

    refs = {ref for ref in (verified.reference, *references) if ref}
    if not refs:
        return None
    return await db.scalar(
        select(AppDownloadRequest.id)
        .where(AppDownloadRequest.payment_reference.in_(refs))
        .limit(1)
    )


async def fulfill_paid_request(
    db: AsyncSession,
    request_id: uuid.UUID,
    verified: VerifiedTransaction,
    provider: PaymentProvider,
) -> AppDownloadRequest | None:
    """Issue the 3-day link for a paid request once its payment is verified.

    ``verified`` is the gateway's own answer, not the webhook body. Returns the
    request when this call fulfilled it (the caller then emails the link), and
    None when there is nothing to do: unknown request, already fulfilled
    (a redelivered webhook), or a payment that does not cover the price.
    """
    req = await db.get(AppDownloadRequest, request_id, with_for_update=True)
    if req is None:
        log.warning("app_download_payment.request_not_found", request_id=str(request_id))
        return None
    if req.status == STATUS_FULFILLED:
        return None

    paid_currency = (verified.currency or "").upper()
    if (
        req.amount is None
        or paid_currency != (req.currency or "").upper()
        or verified.amount < Decimal(req.amount)
    ):
        log.warning(
            "app_download_payment.amount_mismatch",
            request_id=str(req.id),
            expected=str(req.amount), expected_currency=req.currency,
            paid=str(verified.amount), paid_currency=paid_currency,
        )
        return None

    now = datetime.now(timezone.utc)
    req.status = STATUS_FULFILLED
    req.paid_at = now
    req.expires_at = now + timedelta(days=LINK_TTL_DAYS)
    req.payment_provider = provider.value
    await db.commit()
    await db.refresh(req)
    return req


# -- redeeming a link ------------------------------------------------------------

def build_download_link(token: str) -> str:
    base = get_settings().api_base_url.rstrip("/")
    return f"{base}/api/v1/app/download/{token}"


def display_name(req: AppDownloadRequest) -> str:
    return req.app_name or DEFAULT_APP_NAME


def _installer_key(os: str) -> str:
    settings = get_settings()
    keys = {
        "macos": settings.app_installer_macos_s3_key,
        "windows": settings.app_installer_windows_s3_key,
    }
    key = keys.get(os)
    if not key:
        raise NotFoundError(
            f"{DEFAULT_APP_NAME} is not available for {OS_LABELS.get(os, os)}"
        )
    return key


async def resolve_file_url(file_url: str, expiry_seconds: int = PRESIGN_TTL_SECONDS) -> str:
    """Turn an app's stored location into something a browser can download."""
    if file_url.startswith(("r2://", "s3://")):
        bucket, _, key = file_url[5:].partition("/")
        presign = (
            s3_service.generate_r2_presigned_url
            if file_url.startswith("r2://")
            else s3_service.generate_presigned_url
        )
        return await presign(key, expiry_seconds=expiry_seconds, bucket=bucket)
    return file_url


async def public_mobile_links(app: DesktopApp) -> dict[str, str]:
    """Direct links for an app's Android and iOS builds, which are always free."""
    links = {}
    for os in sorted(FREE_PLATFORMS):
        file_url = (app.platforms or {}).get(os)
        if file_url:
            links[os] = await resolve_file_url(file_url, expiry_seconds=PUBLIC_LINK_TTL_SECONDS)
    return links


async def redeem(db: AsyncSession, token: str) -> str:
    """Validate a token and return a short-lived installer URL."""
    req = await db.scalar(
        select(AppDownloadRequest).where(AppDownloadRequest.token == token)
    )
    if req is None:
        raise NotFoundError("Invalid or unknown download link")
    if req.status != STATUS_FULFILLED or req.expires_at is None:
        raise AppError("Payment for this download has not been completed", status_code=402)
    if req.expires_at < datetime.now(timezone.utc):
        raise AppError("This download link has expired", status_code=410)

    if req.app_name is not None:
        # An app-specific link; never fall back to the default installer.
        file_url = (req.app.platforms or {}).get(req.os) if req.app is not None else None
        if file_url is None:
            raise AppError("This app is no longer available for this platform", status_code=410)
        url = await resolve_file_url(file_url)
    else:
        key = _installer_key(req.os)
        url = await s3_service.generate_r2_presigned_url(key, expiry_seconds=PRESIGN_TTL_SECONDS)

    req.last_redeemed_at = datetime.now(timezone.utc)
    await db.commit()
    return url
