from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class VerifiedSubscription:
    """Normalised result of verifying a store receipt/token.

    ``status`` is one of active | grace | expired | canceled | revoked and maps
    directly onto ``IapSubscriptionStatus``. ``store_transaction_id`` is the
    stable idempotency key — Apple's original_transaction_id or the Play
    purchase token.
    """

    store_transaction_id: str
    product_id: str
    expires_at: datetime | None
    status: str
    latest_receipt: str
    raw_payload: dict = field(default_factory=dict)
