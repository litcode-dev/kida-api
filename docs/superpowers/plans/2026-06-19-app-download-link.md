# App Download Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a public endpoint where anyone submits an email + OS and receives an emailed desktop-app download link that expires after 3 days.

**Architecture:** Two public endpoints under `/api/v1/app`. `POST /download-request` persists a row in a new `app_download_requests` table with a random token and `expires_at = now + 3 days`, then emails a tokenized link. `GET /download/{token}` validates the token, then 302-redirects to a short-lived (5 min) S3 presigned URL for the requested OS's installer. The 3-day window lives in the DB; the S3 URL is always short-lived.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2, slowapi (rate limiting), boto3 (S3), pytest/pytest-asyncio.

## Global Constraints

- All success responses use the envelope `{status, data, message}` via `app.schemas.common.success(...)`.
- Errors are raised as `AppError`/`NotFoundError` (from `app.exceptions`) — the global handler renders the envelope. Never return ad-hoc error JSON.
- New models extend `app.database.Base` using `Mapped[...]` / `mapped_column(...)` typed style (see `app/models/newsletter.py`).
- Datetimes are timezone-aware UTC: store `DateTime(timezone=True)`, compute with `datetime.now(timezone.utc)`.
- OS values are exactly `"macos"` and `"windows"` (lowercase) everywhere.
- Tests require a Postgres test DB at `postgresql+asyncpg://litmusic:litmusic@localhost:5432/litmusic_test`; `conftest.py` auto-skips if unavailable. Run with `pytest`.
- Email is sent via `app.services.email_service.send_email(to, subject, html, text)` (async). Templates are plain functions returning strings, branded like the existing `newsletter_subscribe_html`.
- S3 presigned URLs come from `app.services.s3_service.generate_presigned_url(key, expiry_seconds)` (async). Do NOT use `get_download_url` (it returns an unsigned CloudFront URL when configured).
- The current Alembic head is `r7s49t28u5v1`; the new migration's `down_revision` must be `r7s49t28u5v1`.

---

### Task 1: Data layer — model, migration, config

**Files:**
- Create: `app/models/app_download_request.py`
- Modify: `app/models/__init__.py` (add import so the model registers with `Base.metadata`)
- Create: `alembic/versions/a7b58c39d4e1_add_app_download_requests.py`
- Modify: `app/config.py` (add two installer-key settings)
- Modify: `.env.example` (document the two new vars)
- Test: `tests/models/test_app_download_request.py`

**Interfaces:**
- Produces: `AppDownloadRequest` ORM model (`app.models.app_download_request`) with columns `id, email, os, token, expires_at, last_redeemed_at, created_at`.
- Produces: settings `app_installer_macos_s3_key: str` and `app_installer_windows_s3_key: str` on `app.config.Settings`.

- [ ] **Step 1: Write the failing test**

Create `tests/models/test_app_download_request.py`:

```python
import pytest
from datetime import datetime, timedelta, timezone

from app.models.app_download_request import AppDownloadRequest


@pytest.mark.asyncio
async def test_can_persist_and_read_back(db_session):
    req = AppDownloadRequest(
        email="user@test.com",
        os="macos",
        token="tok-abc",
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    db_session.add(req)
    await db_session.commit()

    fetched = await db_session.get(AppDownloadRequest, req.id)
    assert fetched is not None
    assert fetched.email == "user@test.com"
    assert fetched.os == "macos"
    assert fetched.token == "tok-abc"
    assert fetched.last_redeemed_at is None
    assert fetched.created_at is not None
```

Also create an empty `tests/models/__init__.py` if `tests/models/` does not exist.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/models/test_app_download_request.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.app_download_request'`

- [ ] **Step 3: Create the model**

Create `app/models/app_download_request.py`:

```python
import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class AppDownloadRequest(Base):
    __tablename__ = "app_download_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    os: Mapped[str] = mapped_column(String(16), nullable=False)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
```

- [ ] **Step 4: Register the model**

Add this line to the end of `app/models/__init__.py`:

```python
from app.models.app_download_request import AppDownloadRequest  # noqa: F401
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/models/test_app_download_request.py -v`
Expected: PASS (or SKIP if the test DB is unavailable — start it and re-run to confirm PASS)

- [ ] **Step 6: Create the Alembic migration**

Create `alembic/versions/a7b58c39d4e1_add_app_download_requests.py`:

```python
"""add app_download_requests table

Revision ID: a7b58c39d4e1
Revises: r7s49t28u5v1
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "a7b58c39d4e1"
down_revision = "r7s49t28u5v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_download_requests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("os", sa.String(16), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_app_download_requests_email", "app_download_requests", ["email"])
    op.create_index("ix_app_download_requests_token", "app_download_requests", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_app_download_requests_token", table_name="app_download_requests")
    op.drop_index("ix_app_download_requests_email", table_name="app_download_requests")
    op.drop_table("app_download_requests")
```

- [ ] **Step 7: Add config settings**

In `app/config.py`, immediately after the `# IAP — Google` block (the `google_service_account_json` line), add:

```python

    # App installers (desktop download)
    app_installer_macos_s3_key: str = "installers/kida-macos.dmg"
    app_installer_windows_s3_key: str = "installers/kida-windows.exe"
```

In `.env.example`, append at the end:

```bash

# App installers (desktop download) — S3 keys of the uploaded installer files
APP_INSTALLER_MACOS_S3_KEY=installers/kida-macos.dmg
APP_INSTALLER_WINDOWS_S3_KEY=installers/kida-windows.exe
```

- [ ] **Step 8: Verify config import works**

Run: `python -c "from app.config import Settings; print('macos' , 'windows')"`
Expected: prints `macos windows` with no import error. (Loading `get_settings()` may require env vars; importing the class is enough here.)

- [ ] **Step 9: Commit**

```bash
git add app/models/app_download_request.py app/models/__init__.py alembic/versions/a7b58c39d4e1_add_app_download_requests.py app/config.py .env.example tests/models/
git commit -m "feat: add app_download_requests model, migration, and installer config"
```

---

### Task 2: Email templates

**Files:**
- Modify: `app/services/email_service.py` (add `app_download_html` and `app_download_text`)
- Test: `tests/services/test_email_service.py` (append cases)

**Interfaces:**
- Produces: `app_download_html(os_label: str, link: str, expires_at: datetime) -> str`
- Produces: `app_download_text(os_label: str, link: str, expires_at: datetime) -> str`
- Consumes: existing module helpers `_brand_footer()` and `_text_footer()` (already defined in `email_service.py`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/test_email_service.py`:

```python
from datetime import datetime, timezone

from app.services.email_service import app_download_html, app_download_text


def test_app_download_html_contains_link_and_os():
    exp = datetime(2026, 6, 22, tzinfo=timezone.utc)
    html = app_download_html("macOS", "https://api.example/api/v1/app/download/tok123", exp)
    assert "https://api.example/api/v1/app/download/tok123" in html
    assert "macOS" in html
    assert "Jun 22, 2026" in html


def test_app_download_text_contains_link_and_os():
    exp = datetime(2026, 6, 22, tzinfo=timezone.utc)
    txt = app_download_text("Windows", "https://api.example/api/v1/app/download/tok123", exp)
    assert "https://api.example/api/v1/app/download/tok123" in txt
    assert "Windows" in txt
    assert "Jun 22, 2026" in txt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/services/test_email_service.py -v -k app_download`
Expected: FAIL with `ImportError: cannot import name 'app_download_html'`

- [ ] **Step 3: Add the template functions**

In `app/services/email_service.py`, add these two functions next to the other template functions (e.g. directly after `newsletter_subscribe_text`):

```python
def app_download_html(os_label: str, link: str, expires_at: datetime) -> str:
    expires = expires_at.strftime("%b %d, %Y")
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px;border-radius:8px 8px 0 0;color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA &nbsp;&middot;&nbsp; DESKTOP APP</td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:8px 32px 32px 32px;">
        <p style="margin:0;font-size:40px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">Download for</p>
        <p style="margin:0;font-size:40px;font-weight:800;line-height:1.05;color:#1FBF62;letter-spacing:-0.02em;">{os_label}.</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 24px 0;font-size:15px;line-height:1.6;color:#333;">
          Here's your download link for the Kida desktop app on <strong>{os_label}</strong>.
          This link expires on <strong>{expires}</strong>.
        </p>
        <a href="{link}"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Download Kida &rarr;
        </a>
        <p style="margin:24px 0 0 0;font-size:12px;line-height:1.6;color:#777;">
          If the button doesn't work, paste this link into your browser:<br>{link}
        </p>
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def app_download_text(os_label: str, link: str, expires_at: datetime) -> str:
    expires = expires_at.strftime("%b %d, %Y")
    return (
        f"Download Kida for {os_label}\n\n"
        f"Here's your download link for the Kida desktop app:\n{link}\n\n"
        f"This link expires on {expires}."
        + _text_footer()
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/services/test_email_service.py -v -k app_download`
Expected: PASS (both new tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/email_service.py tests/services/test_email_service.py
git commit -m "feat: add desktop app download email templates"
```

---

### Task 3: Service — create + redeem

**Files:**
- Create: `app/services/app_download_service.py`
- Test: `tests/services/test_app_download_service.py`

**Interfaces:**
- Consumes: `AppDownloadRequest` (Task 1); settings `app_installer_macos_s3_key`/`app_installer_windows_s3_key` (Task 1); `s3_service.generate_presigned_url`; `AppError`, `NotFoundError`.
- Produces:
  - `OS_LABELS: dict[str, str]` = `{"macos": "macOS", "windows": "Windows"}`
  - `async create_request(db, email: str, os: str) -> AppDownloadRequest`
  - `build_download_link(token: str) -> str`
  - `async redeem(db, token: str) -> str` (returns a presigned URL; raises `NotFoundError` for unknown token, `AppError(status_code=410)` for expired, `AppError(status_code=503)` if the OS installer key is unconfigured)

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_app_download_service.py`:

```python
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from app.models.app_download_request import AppDownloadRequest
from app.services import app_download_service, s3_service
from app.exceptions import AppError, NotFoundError


@pytest.mark.asyncio
async def test_create_request_sets_3_day_expiry(db_session):
    req = await app_download_service.create_request(db_session, "user@test.com", "macos")
    assert req.token
    assert req.os == "macos"
    delta = req.expires_at - datetime.now(timezone.utc)
    assert timedelta(days=2, hours=23) < delta <= timedelta(days=3)


def test_build_download_link(monkeypatch):
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "api_base_url", "https://api.example/")
    link = app_download_service.build_download_link("tok123")
    assert link == "https://api.example/api/v1/app/download/tok123"


@pytest.mark.asyncio
async def test_redeem_valid_returns_presigned_url(db_session, monkeypatch):
    monkeypatch.setattr(s3_service, "generate_presigned_url", AsyncMock(return_value="https://s3/signed"))
    req = await app_download_service.create_request(db_session, "u@test.com", "windows")
    url = await app_download_service.redeem(db_session, req.token)
    assert url == "https://s3/signed"
    refreshed = await db_session.get(AppDownloadRequest, req.id)
    assert refreshed.last_redeemed_at is not None


@pytest.mark.asyncio
async def test_redeem_unknown_token_raises_not_found(db_session):
    with pytest.raises(NotFoundError):
        await app_download_service.redeem(db_session, "does-not-exist")


@pytest.mark.asyncio
async def test_redeem_expired_token_raises_410(db_session):
    req = AppDownloadRequest(
        email="e@test.com",
        os="macos",
        token="expired-token",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add(req)
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await app_download_service.redeem(db_session, "expired-token")
    assert exc.value.status_code == 410
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/services/test_app_download_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.app_download_service'`

- [ ] **Step 3: Implement the service**

Create `app/services/app_download_service.py`:

```python
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import AppError, NotFoundError
from app.models.app_download_request import AppDownloadRequest
from app.services import s3_service

LINK_TTL_DAYS = 3
PRESIGN_TTL_SECONDS = 300

OS_LABELS = {"macos": "macOS", "windows": "Windows"}


def _installer_key(os: str) -> str:
    settings = get_settings()
    keys = {
        "macos": settings.app_installer_macos_s3_key,
        "windows": settings.app_installer_windows_s3_key,
    }
    key = keys.get(os)
    if not key:
        raise AppError("Installer is not available for the requested OS", status_code=503)
    return key


async def create_request(db: AsyncSession, email: str, os: str) -> AppDownloadRequest:
    """Persist a download request with a 3-day expiry and return it."""
    req = AppDownloadRequest(
        email=email,
        os=os,
        token=secrets.token_urlsafe(32),
        expires_at=datetime.now(timezone.utc) + timedelta(days=LINK_TTL_DAYS),
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)
    return req


def build_download_link(token: str) -> str:
    base = get_settings().api_base_url.rstrip("/")
    return f"{base}/api/v1/app/download/{token}"


async def redeem(db: AsyncSession, token: str) -> str:
    """Validate a token and return a short-lived presigned installer URL."""
    req = await db.scalar(
        select(AppDownloadRequest).where(AppDownloadRequest.token == token)
    )
    if req is None:
        raise NotFoundError("Invalid or unknown download link")
    if req.expires_at < datetime.now(timezone.utc):
        raise AppError("This download link has expired", status_code=410)

    key = _installer_key(req.os)
    url = await s3_service.generate_presigned_url(key, expiry_seconds=PRESIGN_TTL_SECONDS)

    req.last_redeemed_at = datetime.now(timezone.utc)
    await db.commit()
    return url
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/services/test_app_download_service.py -v`
Expected: PASS (all five tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/app_download_service.py tests/services/test_app_download_service.py
git commit -m "feat: add app download service (create + redeem with 3-day expiry)"
```

---

### Task 4: Schema, router, and wiring

**Files:**
- Create: `app/schemas/app_download.py`
- Create: `app/routers/app_download.py`
- Modify: `app/main.py` (import router, include it, add tag metadata)
- Test: `tests/routers/test_app_download.py`

**Interfaces:**
- Consumes: `app_download_service.create_request/build_download_link/redeem/OS_LABELS` (Task 3); `app_download_html/app_download_text/send_email` (Task 2); `limiter`; `success`.
- Produces: `POST /api/v1/app/download-request` and `GET /api/v1/app/download/{token}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/routers/test_app_download.py`:

```python
import pytest
from unittest.mock import AsyncMock
from sqlalchemy import select

from app.models.app_download_request import AppDownloadRequest


@pytest.mark.asyncio
async def test_request_download_sends_email(client, monkeypatch):
    import app.routers.app_download as mod
    mock_send = AsyncMock()
    monkeypatch.setattr(mod, "send_email", mock_send)

    resp = await client.post(
        "/api/v1/app/download-request",
        json={"email": "a@test.com", "os": "macos"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["data"]["email"] == "a@test.com"
    assert body["data"]["os"] == "macos"
    mock_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_download_rejects_unsupported_os(client):
    resp = await client.post(
        "/api/v1/app/download-request",
        json={"email": "a@test.com", "os": "linux"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_request_download_rejects_invalid_email(client):
    resp = await client.post(
        "/api/v1/app/download-request",
        json={"email": "not-an-email", "os": "macos"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_redeem_redirects_to_installer(client, db_session, monkeypatch):
    import app.routers.app_download as mod
    monkeypatch.setattr(mod, "send_email", AsyncMock())
    from app.services import s3_service
    monkeypatch.setattr(
        s3_service, "generate_presigned_url", AsyncMock(return_value="https://s3/installer.exe")
    )

    await client.post(
        "/api/v1/app/download-request",
        json={"email": "b@test.com", "os": "windows"},
    )
    req = await db_session.scalar(
        select(AppDownloadRequest).where(AppDownloadRequest.email == "b@test.com")
    )

    resp = await client.get(f"/api/v1/app/download/{req.token}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://s3/installer.exe"


@pytest.mark.asyncio
async def test_redeem_unknown_token_returns_404(client):
    resp = await client.get("/api/v1/app/download/nonexistent-token", follow_redirects=False)
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/routers/test_app_download.py -v`
Expected: FAIL — the route does not exist yet (404 from FastAPI for the POST, or import error for `app.routers.app_download`).

- [ ] **Step 3: Create the request schema**

Create `app/schemas/app_download.py`:

```python
from typing import Literal

from pydantic import BaseModel, EmailStr


class AppDownloadRequestBody(BaseModel):
    email: EmailStr
    os: Literal["macos", "windows"]
```

- [ ] **Step 4: Create the router**

Create `app/routers/app_download.py`:

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.rate_limit import limiter
from app.schemas.app_download import AppDownloadRequestBody
from app.schemas.common import success
from app.services import app_download_service
from app.services.app_download_service import OS_LABELS
from app.services.email_service import send_email, app_download_html, app_download_text

router = APIRouter(prefix="/app", tags=["app"])


@router.post(
    "/download-request",
    summary="Request a desktop app download link",
    description=(
        "Public endpoint. Submit an email address and operating system to receive "
        "a download link for the Kida desktop app. The link expires after 3 days."
    ),
    responses={
        200: {"description": "Download link sent"},
        422: {"description": "Invalid email or unsupported OS"},
    },
)
@limiter.limit("5/hour")
async def request_download(
    request: Request,
    body: AppDownloadRequestBody,
    db: AsyncSession = Depends(get_db),
):
    req = await app_download_service.create_request(db, body.email, body.os)
    link = app_download_service.build_download_link(req.token)
    os_label = OS_LABELS[body.os]
    await send_email(
        to=body.email,
        subject=f"Your Kida download link for {os_label}",
        html=app_download_html(os_label, link, req.expires_at),
        text=app_download_text(os_label, link, req.expires_at),
    )
    return success(
        message="Download link sent",
        data={"email": body.email, "os": body.os},
    )


@router.get(
    "/download/{token}",
    summary="Redeem a desktop app download link",
    description=(
        "Public endpoint. Redirects to a short-lived installer download URL when the "
        "token is valid and unexpired."
    ),
    responses={
        302: {"description": "Redirect to the installer download"},
        404: {"description": "Unknown download link"},
        410: {"description": "Download link has expired"},
    },
)
async def redeem_download(token: str, db: AsyncSession = Depends(get_db)):
    url = await app_download_service.redeem(db, token)
    return RedirectResponse(url, status_code=302)
```

- [ ] **Step 5: Wire into `app/main.py`**

In the routers import line, add `app_download`:

```python
from app.routers import auth, loops, stem_packs, payments, admin, downloads, likes, subscriptions, ai, drones, drum_kits, purchases, producer, newsletter, push_notifications, app_download
```

In the `_tags_metadata` list, add (e.g. after the `push_notifications`/`health` area — place it before the `health` entry):

```python
    {"name": "app", "description": "Public desktop app download requests."},
```

After the existing `app.include_router(push_notifications.router, prefix=PREFIX)` line, add:

```python
app.include_router(app_download.router, prefix=PREFIX)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/routers/test_app_download.py -v`
Expected: PASS (all five tests)

- [ ] **Step 7: Run the full suite to check for regressions**

Run: `pytest -q`
Expected: All tests pass or skip (no new failures). The app imports cleanly.

- [ ] **Step 8: Commit**

```bash
git add app/schemas/app_download.py app/routers/app_download.py app/main.py tests/routers/test_app_download.py
git commit -m "feat: add public app download-request and redeem endpoints"
```

---

## Post-implementation (manual, not code)

- Upload the actual installers to S3 at the keys configured in env:
  `installers/kida-macos.dmg` and `installers/kida-windows.exe` (override via
  `APP_INSTALLER_MACOS_S3_KEY` / `APP_INSTALLER_WINDOWS_S3_KEY`).
- Ensure `API_BASE_URL` is set in the deployed environment so emailed links are absolute.
- Apply the migration in each environment: `alembic upgrade head`.

## Self-Review Notes

- **Spec coverage:** public endpoint (Task 4), email with link (Tasks 2+4), 3-day expiry (Task 3 `LINK_TTL_DAYS`), OS→installer mapping via config (Tasks 1+3), tokenized redeem → presigned redirect (Tasks 3+4), rate limiting (Task 4 `5/hour`), error envelope for expired/unknown (Task 3 raises `AppError`/`NotFoundError`). All covered.
- **Type consistency:** `os` values `"macos"`/`"windows"`, `OS_LABELS` keys match the `Literal`, `generate_presigned_url(key, expiry_seconds=...)` matches `s3_service`, `create_request`/`redeem`/`build_download_link` signatures consistent across service and router.
