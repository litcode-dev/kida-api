import socket

import asyncpg
import kombu.exceptions
import redis.exceptions
import sentry_sdk
import sqlalchemy.exc
import structlog
from fastapi import Request, status
from fastapi.responses import JSONResponse

log = structlog.get_logger()


class AppError(Exception):
    def __init__(self, message: str, status_code: int = 400, data=None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, message: str = "Not found"):
        super().__init__(message, status_code=status.HTTP_404_NOT_FOUND)


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Unauthorized"):
        super().__init__(message, status_code=status.HTTP_401_UNAUTHORIZED)


class ForbiddenError(AppError):
    def __init__(self, message: str = "Forbidden"):
        super().__init__(message, status_code=status.HTTP_403_FORBIDDEN)


class ConflictError(AppError):
    def __init__(self, message: str = "Conflict"):
        super().__init__(message, status_code=status.HTTP_409_CONFLICT)


class EmailNotVerifiedError(AppError):
    def __init__(self, message: str = "Email not verified"):
        # data.is_verified lets clients detect this case from the body rather
        # than parsing the message or relying on the 403 status alone.
        super().__init__(message, status_code=status.HTTP_403_FORBIDDEN, data={"is_verified": False})


class PaymentError(AppError):
    def __init__(self, message: str = "Payment failed"):
        super().__init__(message, status_code=status.HTTP_402_PAYMENT_REQUIRED)


class EntitlementError(AppError):
    def __init__(self, message: str = "Purchase required to access this file"):
        super().__init__(message, status_code=status.HTTP_403_FORBIDDEN)


class FreeTierLimitError(AppError):
    """A free-tier download cap was hit. Rendered with a stable machine-readable
    body (see free_tier_limit_handler) so the app can show the paywall directly.
    """

    def __init__(self, item_type: str, limit: int):
        self.item_type = item_type
        self.limit = limit
        super().__init__(
            f"Free-tier {item_type} download limit reached ({limit})",
            status_code=status.HTTP_403_FORBIDDEN,
        )


class MonthlyDownloadLimitError(AppError):
    """A monthly download allowance was used up. Rendered with its own stable
    machine-readable body (see monthly_limit_handler) so the app can tell this
    apart from the free-tier paywall — this one is not fixed by subscribing, it
    resets at the start of next month.
    """

    def __init__(self, item_type: str, limit: int, resets_at: str):
        self.item_type = item_type
        self.limit = limit
        self.resets_at = resets_at
        # Always 0 here: the limit is only raised once credits run out. Sent
        # anyway so the client can key its top-up prompt off one stable field.
        self.extra_credits = 0
        super().__init__(
            f"Monthly {item_type} download limit reached ({limit})",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )


async def monthly_limit_handler(
    request: Request, exc: MonthlyDownloadLimitError
) -> JSONResponse:
    # Stable contract parsed by the app — do not change field names or shape.
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "monthly_download_limit",
            "type": exc.item_type,
            "limit": exc.limit,
            "resets_at": exc.resets_at,
            "extra_credits": exc.extra_credits,
        },
    )


async def free_tier_limit_handler(request: Request, exc: FreeTierLimitError) -> JSONResponse:
    # Stable contract parsed by the app — do not change field names or shape.
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "free_tier_limit", "type": exc.item_type, "limit": exc.limit},
    )


# Losing Redis or Postgres is not a bug in the request — it is the API being
# unable to serve anything — so it gets a 503 the client can retry rather than
# the catch-all's "An unexpected error occurred".
#
# Which exceptions actually arrive was measured, not assumed, because with
# asyncpg most of them never become a SQLAlchemy error at all:
#
#   Postgres refusing connections  -> builtins.ConnectionRefusedError
#   Postgres host unresolvable     -> socket.gaierror
#   Postgres backend killed        -> asyncpg InternalClientError
#   Postgres shutting down/starting-> asyncpg OperatorInterventionError
#   connect() hanging              -> builtins.TimeoutError (asyncio's)
#   Redis unreachable              -> redis.exceptions.ConnectionError
#   Celery broker unreachable      -> kombu.exceptions.OperationalError
#
# Deliberately absent: sqlalchemy.exc.DBAPIError and ProgrammingError, and
# redis.exceptions.ResponseError. A statement timeout, a value too long for its
# column, a malformed query and a bad command all land there — those are bugs
# in our code, and they must keep failing loudly as 500s instead of being
# dressed up as an outage.
SERVICE_UNAVAILABLE_ERRORS = (
    # Connection-level failures from any driver that wraps its own.
    sqlalchemy.exc.InterfaceError,
    sqlalchemy.exc.OperationalError,
    asyncpg.exceptions.PostgresConnectionError,
    # The server is going away or not yet accepting: shutting down, crash
    # recovery, still starting up.
    asyncpg.exceptions.OperatorInterventionError,
    # Out of connections, memory or disk — the database cannot serve this now,
    # which is the same answer to the client as being down.
    asyncpg.exceptions.InsufficientResourcesError,
    asyncpg.exceptions.InternalClientError,
    redis.exceptions.ConnectionError,  # BusyLoadingError included
    redis.exceptions.TimeoutError,
    # Celery cannot reach its broker, so .delay() raises instead of queueing.
    # Catching it here means a handler that queues work reports an honest
    # "try again" rather than a 500, without every call site guarding itself.
    kombu.exceptions.OperationalError,
    # Raw socket failures, which is how asyncpg reports an unreachable server.
    # Narrower than OSError on purpose: FileNotFoundError is a bug, not an outage.
    ConnectionError,
    socket.gaierror,
    TimeoutError,
)


def _failed_dependency(exc: Exception) -> str:
    if isinstance(exc, kombu.exceptions.OperationalError):
        return "celery-broker"
    if isinstance(exc, (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)):
        return "redis"
    if isinstance(exc, (
        sqlalchemy.exc.SQLAlchemyError,
        asyncpg.exceptions.PostgresError,
        asyncpg.exceptions.InternalClientError,
    )):
        return "database"
    # A raw socket error, which reaches here almost only from asyncpg: redis-py
    # and httpx both wrap theirs in their own types. Not called "database"
    # because nothing here proves it — the address is in the logged message.
    return "network"


async def service_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a lost backing service as a retryable 503.

    Sentry is notified explicitly: its Starlette integration auto-reports a
    handled exception only when the exception itself carries a 5xx status_code
    attribute, which none of these do, so registering a handler for them would
    otherwise take outages out of alerting entirely.
    """
    dependency = _failed_dependency(exc)
    log.error(
        "service_unavailable",
        dependency=dependency,
        path=request.url.path,
        method=request.method,
        exc_type=type(exc).__name__,
        exc=str(exc),
        exc_info=True,
    )
    sentry_sdk.capture_exception(exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "error",
            "data": None,
            "message": "Service temporarily unavailable. Please try again in a moment.",
        },
        headers={"Retry-After": "5"},
    )


# SQLSTATE 22001 (string_data_right_truncation): a value was longer than its
# column. Matched on the SQLSTATE rather than a driver's exception class
# because asyncpg's error arrives wrapped twice — SQLAlchemy's DBAPIError holds
# the dialect's Error, which holds the asyncpg one as its __cause__.
_STRING_TRUNCATION_SQLSTATE = "22001"


def _sqlstate(exc: Exception) -> str | None:
    orig = getattr(exc, "orig", None)
    for candidate in (exc, orig, getattr(orig, "__cause__", None)):
        state = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if state:
            return str(state)
    return None


def unexpected_error_response(
    request: Request, exc: Exception, capture: bool = False
) -> JSONResponse:
    """The 500 of last resort. `capture` is for callers reached through a
    registered handler, which Sentry does not report on its own."""
    log.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        exc_type=type(exc).__name__,
        exc=str(exc),
        exc_info=True,
    )
    if capture:
        sentry_sdk.capture_exception(exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"status": "error", "data": None, "message": "An unexpected error occurred"},
    )


async def dbapi_error_handler(
    request: Request, exc: sqlalchemy.exc.DBAPIError
) -> JSONResponse:
    """Answer 422 for a value too long for its column; 500 for everything else.

    The request schemas carry max_length wherever a field maps to a bounded
    column, so this is the backstop for what they cannot cover: a field added
    later, or a value the API derives rather than receives. It cannot name the
    offending field — Postgres reports the column type and nothing else — which
    is why the schema bounds stay the first line of defence and this is only
    the net beneath them.

    Every other DBAPIError still returns 500. A statement timeout, a malformed
    query, a constraint violation: those are our bugs, and dressing them as
    client errors would hide them.
    """
    if _sqlstate(exc) == _STRING_TRUNCATION_SQLSTATE:
        log.warning(
            "value_too_long_for_column",
            path=request.url.path,
            method=request.method,
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "status": "error",
                "data": None,
                "message": "One of the submitted values is too long.",
            },
        )
    return unexpected_error_response(request, exc, capture=True)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "data": exc.data, "message": exc.message},
    )


def validation_error_422(exc) -> AppError:
    """Render a pydantic ValidationError as the API's own 422 envelope.

    Filter models are built inside the handler rather than declared as the
    signature, so a failure arrives as a bare ValidationError — which the
    catch-all handler would log and return as a 500. Reporting the first
    error's original message keeps the body readable ("Time signature ..."
    rather than pydantic's "Value error, Time signature ...").
    """
    err = exc.errors()[0]
    return AppError(
        str(err.get("ctx", {}).get("error", err["msg"])),
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
