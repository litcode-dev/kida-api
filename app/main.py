import json
from contextlib import asynccontextmanager

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.config import get_settings
from app.exceptions import (
    AppError, FreeTierLimitError, MonthlyDownloadLimitError,
    SERVICE_UNAVAILABLE_ERRORS,
    app_error_handler, dbapi_error_handler, free_tier_limit_handler,
    monthly_limit_handler, service_unavailable_handler, unexpected_error_response,
)
import sqlalchemy.exc
from app.middleware.logging_middleware import LoggingMiddleware
from app.middleware.rate_limit import limiter
from app.routers import auth, loops, stem_packs, payments, admin, downloads, likes, subscriptions, ai, drones, drum_kits, purchases, producer, newsletter, push_notifications, app_download, loop_requests

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)

settings = get_settings()

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        send_default_pii=True,
        integrations=[StarletteIntegration(), FastApiIntegration()],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the digest's safety net alongside the API.

    The daily mail is scheduled in celery beat, a separate process that serves
    no traffic — so a deployment without one looks entirely healthy while no
    digest is ever sent. The API checks the same schedule and sends the digest
    itself if nothing else has; the run is claimed through a unique row, so beat
    and any number of replicas still produce one email.
    """
    from app.services import digest_scheduler

    digest_scheduler.start()
    try:
        yield
    finally:
        await digest_scheduler.stop()


_tags_metadata = [
    {"name": "auth", "description": "Registration, login, token refresh, and OAuth (Google, Apple)."},
    {"name": "loops", "description": "Browse and download individual audio loops."},
    {
        "name": "stem-packs",
        "description": (
            "Browse and download multi-stem packs. A pack is either long-form — one "
            "continuous stem per instrument — or a breakdown, split into song parts that "
            "an arrangement stitches back into a full song."
        ),
    },
    {"name": "drum-kits", "description": "Browse and download drum kits with individual sample files."},
    {"name": "drones", "description": "Browse and download drone pad audio files."},
    {"name": "payments", "description": "Initiate and verify purchases via Flutterwave or Paystack."},
    {"name": "purchases", "description": "View the authenticated user's purchase history."},
    {"name": "downloads", "description": "Retrieve signed download URLs for purchased content."},
    {"name": "likes", "description": "Like and unlike loops."},
    {"name": "subscriptions", "description": "Manage user subscriptions."},
    {"name": "ai", "description": "AI-assisted loop generation (requires AI-enabled account)."},
    {"name": "admin", "description": "Content management and user administration. Requires producer or admin role."},
    {"name": "producer", "description": "Producer earnings and download analytics."},
    {"name": "app", "description": "Public desktop app download requests."},
    {"name": "loop-requests", "description": "Authenticated user requests for new loops."},
    {"name": "health", "description": "Service health check."},
]

app = FastAPI(
    title="LitMusic API",
    version="1.0.0",
    description=(
        "Backend API for the LitMusic marketplace. "
        "All responses use the envelope format: `{status, data, message}`. "
        "Authenticated endpoints require a Bearer token obtained from `/api/v1/auth/login`."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=_tags_metadata,
    lifespan=lifespan,
    swagger_ui_parameters={
        "persistAuthorization": True,
        "tryItOutEnabled": True,
        "displayRequestDuration": True,
        "defaultModelsExpandDepth": -1,
    },
)

# Rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

def _parse_origins(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("["):
        return json.loads(raw)
    return [o.strip() for o in raw.split(",") if o.strip()]


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_origins(settings.allowed_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Logging
app.add_middleware(LoggingMiddleware)

# Error handlers — all responses use the same envelope format
app.add_exception_handler(AppError, app_error_handler)
# Except free-tier cap refusals, which use a stable machine-readable body the
# app parses to show the paywall (most-derived handler wins over AppError's).
app.add_exception_handler(FreeTierLimitError, free_tier_limit_handler)
app.add_exception_handler(MonthlyDownloadLimitError, monthly_limit_handler)

# Redis or Postgres being unreachable answers 503 instead of falling through to
# the catch-all below. See SERVICE_UNAVAILABLE_ERRORS for what does and does not
# count as an outage.
for _exc in SERVICE_UNAVAILABLE_ERRORS:
    app.add_exception_handler(_exc, service_unavailable_handler)

# A value too long for its column answers 422; every other database error keeps
# its 500. Registered on the base class, so the outage subclasses above still
# win for their own types.
app.add_exception_handler(sqlalchemy.exc.DBAPIError, dbapi_error_handler)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "data": None, "message": str(exc.detail)},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = [f"{' -> '.join(str(l) for l in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"status": "error", "data": None, "message": "; ".join(errors)},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # No explicit Sentry capture here: Starlette re-raises after this handler
    # runs, so the ASGI integration reports it.
    return unexpected_error_response(request, exc)

# Routers
PREFIX = "/api/v1"
app.include_router(auth.router, prefix=PREFIX)
app.include_router(loops.router, prefix=PREFIX)
app.include_router(stem_packs.router, prefix=PREFIX)
app.include_router(payments.router, prefix=PREFIX)
app.include_router(admin.router, prefix=PREFIX)
app.include_router(downloads.router, prefix=PREFIX)
app.include_router(likes.router, prefix=PREFIX)
app.include_router(subscriptions.router, prefix=PREFIX)
app.include_router(ai.router, prefix=PREFIX)
app.include_router(drones.router, prefix=PREFIX)
app.include_router(drum_kits.router, prefix=PREFIX)
app.include_router(purchases.router, prefix=PREFIX)
app.include_router(producer.router, prefix=PREFIX)
app.include_router(newsletter.router, prefix=PREFIX)
app.include_router(push_notifications.router, prefix=PREFIX)
app.include_router(app_download.router, prefix=PREFIX)
app.include_router(loop_requests.router, prefix=PREFIX)


@app.get("/health", tags=["health"])
async def health_check():
    from app.database import engine
    from redis.asyncio import Redis
    try:
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    redis_error = None
    try:
        r = Redis.from_url(settings.redis_url)
        await r.ping()
        await r.aclose()
        redis_ok = True
    except Exception as e:
        redis_ok = False
        redis_error = str(e)

    overall = "healthy" if db_ok and redis_ok else "degraded"
    return {
        "status": overall,
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else f"error: {redis_error}",
    }
