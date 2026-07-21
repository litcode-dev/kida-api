import hashlib
import json

import structlog

from app.exceptions import AppError

logger = structlog.get_logger()


async def verify_apple_receipt(receipt: str) -> str:
    """Store-side verification with Apple is disabled — the client's receipt
    is trusted as proof of purchase without confirming it with Apple.

    Returns a stable id derived from the receipt so re-posting the same
    receipt stays idempotent.
    """
    if not receipt:
        raise AppError("Missing receipt", status_code=402)
    logger.info("apple_receipt_trusted_unverified")
    return hashlib.sha256(receipt.encode()).hexdigest()


async def verify_google_receipt(receipt: str) -> str:
    """Store-side verification with Google is disabled — the client's
    purchase token is trusted as proof of purchase without confirming it
    with the Play Developer API.
    """
    try:
        payload = json.loads(receipt)
    except Exception:
        raise AppError("Invalid Android receipt format", status_code=402)

    purchase_token = payload.get("purchaseToken", "")
    if not purchase_token:
        raise AppError("Missing purchaseToken in Android receipt", status_code=402)

    logger.info("google_receipt_trusted_unverified")
    return purchase_token
