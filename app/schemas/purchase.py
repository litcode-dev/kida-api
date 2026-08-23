import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, model_validator

from app.models.purchase import PaymentProvider


class CheckoutRequest(BaseModel):
    loop_id: uuid.UUID | None = None
    stem_pack_id: uuid.UUID | None = None
    # Any gateway in the registry; the factory turns the name into an adapter.
    provider: PaymentProvider = PaymentProvider.flutterwave

    @model_validator(mode="after")
    def exactly_one_product(self) -> "CheckoutRequest":
        if bool(self.loop_id) == bool(self.stem_pack_id):
            raise ValueError("Provide exactly one of loop_id or stem_pack_id")
        return self


class PurchaseResponse(BaseModel):
    id: uuid.UUID
    loop_id: uuid.UUID | None
    stem_pack_id: uuid.UUID | None
    amount_paid: Decimal
    created_at: datetime

    model_config = {"from_attributes": True}


class CheckoutResponse(BaseModel):
    checkout_url: str
    payment_reference: str
