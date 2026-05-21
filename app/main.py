import json
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
from app.exceptions import AppError, app_error_handler
from app.middleware.logging_middleware import LoggingMiddleware
from app.middleware.rate_limit import limiter
from app.routers import auth, loops, stem_packs, payments, admin, downloads, likes, subscriptions, ai, drones, drum_kits, purchases, producer, newsletter, push_notifications

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

_tags_metadata = [
    {"name": "auth", "description": "Registration, login, token refresh, and OAuth (Google, Apple)."},
    {"name": "loops", "description": "Browse and download individual audio loops."},
    {"name": "stem-packs", "description": "Browse and download multi-stem packs."},
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
    import structlog as _structlog
    _structlog.get_logger().error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        exc_type=type(exc).__name__,
        exc=str(exc),
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"status": "error", "data": None, "message": "An unexpected error occurred"},
    )

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
