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
