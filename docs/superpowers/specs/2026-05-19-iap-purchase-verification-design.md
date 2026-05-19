# In-App Purchase Verification & Ownership — Design Spec

**Date:** 2026-05-19

## Overview

Add two endpoints to support mobile in-app purchases (Apple App Store and Google Play Store). The mobile Flutter app calls these after a successful store purchase to verify the receipt server-side and record ownership.

This is a separate system from the existing Flutterwave/Paystack web payment flow. It uses a new `iap_purchases` table.

---

## Endpoints

### GET /api/v1/purchases/me

- **Auth:** Bearer token required
- **Returns:** list of item UUIDs the authenticated user owns (verified IAP purchases)
- **Always returns 200**, even when the user has no purchases (empty list, not 404)

**Response:**
```json
{
  "status": "success",
  "data": { "owned_item_ids": ["uuid-1", "uuid-2"] },
  "message": ""
}
```

---

### POST /api/v1/purchases/verify

- **Auth:** Bearer token required
- **Purpose:** Verify a store receipt, then upsert ownership record

**Request body:**
```json
{
  "item_id": "uuid-of-item-in-db",
  "platform": "ios",
  "receipt": "<base64 receipt or Android JSON payload>"
}
```

**Android receipt format** — JSON string: `{"productId": "com.litmusic.loop_xxx", "purchaseToken": "..."}`

**Responses:**
- `200 {}` — verified and recorded
- `422` — malformed receipt (cannot parse)
- `402` — store rejected the receipt (Apple status ≠ 0 / Android purchaseState ≠ 0)

**Idempotent:** Uses upsert on `(user_id, item_id)` — safe to call multiple times (app retries on network failure).

---

## Database

New table — does not modify existing `purchases` table.

```sql
CREATE TABLE iap_purchases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id),
    item_id     UUID NOT NULL,
    platform    TEXT NOT NULL CHECK (platform IN ('ios', 'android')),
    receipt     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'verified',
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, item_id)
);
CREATE INDEX ON iap_purchases (user_id);
```

`item_id` has no FK constraint — it covers all content types (loops, drum kits, drones, stem packs) which share a UUID namespace.

---

## New Files

| File | Role |
|---|---|
| `app/models/iap_purchase.py` | SQLAlchemy `IAPPurchase` model |
| `app/schemas/iap_purchase.py` | `VerifyReceiptRequest`, `OwnedItemsResponse` Pydantic schemas |
| `app/services/iap_service.py` | Apple + Google verification logic, DB upsert |
| `app/routers/purchases.py` | Route handlers |

**Modified files:**
- `app/config.py` — 3 new optional settings
- `app/main.py` — register `purchases` router

---

## Configuration (new fields, all optional with empty defaults)

```
APPLE_SHARED_SECRET=       # Apple shared secret for receipt validation
APPLE_BUNDLE_ID=           # e.g. com.litmusic.app
GOOGLE_SERVICE_ACCOUNT_JSON=  # full JSON string of service account key
GOOGLE_PLAY_PACKAGE_NAME=  # e.g. com.litmusic.app
```

---

## Verification Logic

### iOS

1. Parse `receipt` as a base64 string (422 if not valid base64)
2. POST to `https://buy.itunes.apple.com/verifyReceipt` with `{"receipt-data": receipt, "password": apple_shared_secret, "exclude-old-transactions": true}`
3. If Apple returns `status == 21007` (sandbox receipt sent to production), retry against `https://sandbox.itunes.apple.com/verifyReceipt`
4. Assert `status == 0` — raise `PaymentError` (402) if not
5. Assert `receipt.bundle_id == settings.apple_bundle_id` — raise `PaymentError` (402) if mismatch

### Android

1. Parse `receipt` as JSON — expect `{"productId": "...", "purchaseToken": "..."}` (422 if malformed)
2. Authenticate with Google using `google_service_account_json` (service account with `androidpublisher` scope)
3. Call `GET https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{packageName}/purchases/products/{productId}/tokens/{purchaseToken}`
4. Assert `purchaseState == 0` — raise `PaymentError` (402) if not

### After successful verification (both platforms)

Upsert into `iap_purchases`:
```sql
INSERT INTO iap_purchases (user_id, item_id, platform, receipt, status, verified_at)
VALUES (...)
ON CONFLICT (user_id, item_id) DO UPDATE SET
  receipt = EXCLUDED.receipt,
  verified_at = now(),
  status = 'verified';
```

---

## Error Handling

| Condition | Status |
|---|---|
| Missing / invalid Bearer token | 401 |
| Receipt cannot be parsed / decoded | 422 |
| Store rejects receipt (bad status/state) | 402 |
| Bundle ID / package name mismatch | 402 |
| Google API auth failure | 500 (logged, not exposed) |

---

## Dependencies

No new packages required:
- `httpx` — already present, used for Apple REST call
- `google-auth` — needed for Google service account OAuth2 token generation (add to requirements.txt)

---

## Out of Scope

- Subscription IAP (not used — project is one-time purchases only)
- Refund webhooks from Apple/Google
- Re-verification of old receipts
- Storing decrypted receipt payload fields (only the raw receipt is stored for audit)
