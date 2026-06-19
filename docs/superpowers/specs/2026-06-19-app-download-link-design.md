# App Download Link — Design

**Date:** 2026-06-19
**Status:** Approved (pending spec review)

## Summary

A public feature that lets anyone request the Kida desktop app installer by
submitting their email address and operating system. The server emails a link
that expires after 3 days. Clicking the link redirects to a short-lived S3
presigned URL for the correct OS installer.

## Goals

- Public, unauthenticated endpoint accepting `{ email, os }`.
- Email the requester a download link.
- Link expires 3 days after creation.
- Serve the correct installer for the requested OS (macOS or Windows).
- Reuse existing infrastructure (S3, email service, rate limiter, error envelope).

## Non-Goals

- Versioned releases / release management UI (YAGNI; upgrade path noted below).
- Admin management of installers (handled by uploading to S3 directly).
- Linux installer (only macOS + Windows for now).
- Single-use links (links are reusable until expiry).

## Where the installers live

The macOS and Windows installers are uploaded to a **Cloudflare R2** bucket
(S3-compatible) under a stable prefix, e.g.:

- `installers/kida-macos.dmg`
- `installers/kida-windows.exe`

A new release is shipped by overwriting these objects; the keys stay constant.
The keys are configured via env vars, not hardcoded. The redeem step generates a
short-lived presigned GET URL against R2 via a dedicated R2 boto3 client
(`s3_service.generate_r2_presigned_url`, `s3v4` signing). The rest of the
marketplace content remains on AWS S3.

## Flow

1. `POST /api/v1/app/download-request` (public, rate-limited)
   - Body: `{ "email": EmailStr, "os": "macos" | "windows" }`.
   - Creates a row in `app_download_requests` with a random `token`, the `os`,
     and `expires_at = now + 3 days`.
   - Sends an email containing the link
     `{API_BASE_URL}/api/v1/app/download/{token}`.
   - Returns a generic success envelope (does not expose whether/where the email
     was delivered).

2. `GET /api/v1/app/download/{token}` (public)
   - Looks up the token. If missing or `expires_at < now`, returns `410 Gone`
     (or `404` for unknown) in the standard `{status, data, message}` envelope.
   - Resolves the installer S3 key for the row's `os`.
   - Generates a short-lived (5 min) S3 presigned URL via
     `s3_service.generate_presigned_url(key, expiry_seconds=300)`.
   - Updates `last_redeemed_at`.
   - Responds with `302` redirect (`RedirectResponse`) to the presigned URL.

The 3-day window lives in the DB row. The S3 URL itself is always short-lived,
so a leaked redirect target expires in minutes; the durable artifact is the
tokenized API link, which the server controls and can expire/revoke.

## Data model

New table `app_download_requests`:

| Column            | Type        | Notes                                  |
|-------------------|-------------|----------------------------------------|
| `id`              | UUID        | Primary key                            |
| `email`           | String      | Indexed                                |
| `os`              | String      | `"macos"` or `"windows"`               |
| `token`           | String      | Unique, indexed; `secrets.token_urlsafe(32)` |
| `expires_at`      | DateTime(tz)| `created_at + 3 days`                  |
| `last_redeemed_at`| DateTime(tz)| Nullable; updated on each successful redeem |
| `created_at`      | DateTime(tz)| Default now                            |

Migration: new Alembic revision adding the table, following the existing
revision style in `alembic/versions/`.

## Components

| File | Responsibility |
|------|----------------|
| `app/models/app_download_request.py` | SQLAlchemy model for `app_download_requests` |
| `alembic/versions/<rev>_add_app_download_requests.py` | Create the table |
| `app/schemas/app_download.py` | `AppDownloadRequest { email: EmailStr, os: Literal["macos","windows"] }` |
| `app/services/app_download_service.py` | `create_request(db, email, os)` → token/link; `redeem(db, token)` → presigned URL. OS→key map from settings. Raises `AppError` for invalid/expired. |
| `app/services/email_service.py` | Add `app_download_html(os, link, expires_at)` and `app_download_text(...)` template functions |
| `app/routers/app_download.py` | The two endpoints, prefix `/app`, tag `app`; POST rate-limited |
| `app/config.py` + `.env.example` | `app_installer_macos_s3_key`, `app_installer_windows_s3_key` |
| `app/main.py` | `include_router(app_download.router)` and add `app` tag metadata |
| `tests/routers/test_app_download.py` | Request → row created + email sent; redeem → 302 to presigned URL; expired/unknown → rejected |

## Key decisions

- **OS→installer mapping via env config** (two vars), not a DB table — simplest,
  matches the current request. Upgrade path: an `app_releases` table if
  versioning or admin management is needed later.
- **Rate limiting** on the POST endpoint (e.g. `5/hour` per IP via the existing
  `slowapi` limiter) — a public endpoint that emails an arbitrary address can be
  abused to email-bomb a victim, so this is required, not optional.
- **Reusable links** — usable for multiple downloads until expiry; `last_redeemed_at`
  is recorded for visibility rather than burning the token on first use.

## Error handling

- Invalid `os` → `422` via existing `RequestValidationError` handler (Literal type).
- Unknown token → `404`; expired token → `410`; both via `AppError` →
  `{status:"error", data:null, message:...}` envelope.
- Missing installer S3 key configuration → `500` AppError with a clear message;
  surfaced in logs.
- Email send failures follow existing `email_service` behavior (logged; the
  request row is still created so the user can retry / the link still works if
  they get the link another way).

## Testing

- `POST` with valid body creates a row with `expires_at ≈ now + 3 days` and
  calls `send_email` (mocked).
- `POST` with invalid `os` → `422`.
- `GET` valid unexpired token → `302` with a presigned URL (s3_service mocked).
- `GET` expired token → `410`; unknown token → `404`.
- Rate limit triggers after the configured threshold.

## Security / abuse considerations

- Rate limit the POST endpoint per IP.
- Generic success response (no user enumeration / delivery confirmation).
- Tokens are unguessable (`token_urlsafe(32)`).
- Redirect target is always a short-lived presigned URL.
