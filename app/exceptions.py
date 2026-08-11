from fastapi import Request, status
from fastapi.responses import JSONResponse


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


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "data": exc.data, "message": exc.message},
    )
