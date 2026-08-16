import asyncio
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger()

_RESEND_URL = "https://api.resend.com/emails"
_RESEND_BATCH_URL = "https://api.resend.com/emails/batch"


async def _send_via_resend(
    settings, to: str, subject: str, html: str, text: str, headers: dict | None = None
) -> None:
    payload = {
        "from": settings.resend_from,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if headers:
        payload["headers"] = headers
    headers = {"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(_RESEND_URL, json=payload, headers=headers)
        resp.raise_for_status()


async def _send_batch_via_resend(settings, messages: list[dict]) -> None:
    """One request per chunk instead of one per address.

    Resend's batch endpoint takes up to 100 messages per call, so a list of
    5,000 goes out in ~50 requests rather than 5,000 sequential ones where a
    single slow response stalls everything behind it. Each message carries its
    own body and headers, because a one-click unsubscribe link is specific to
    the address it was sent to.
    """
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(_RESEND_BATCH_URL, json=messages, headers=headers)
        resp.raise_for_status()


async def send_bulk_email(
    recipients: list[str],
    subject: str,
    html: str | None = None,
    text: str | None = None,
    *,
    build=None,
) -> tuple[int, int]:
    """Send to many addresses. Returns (sent, failed).

    ``build(address)`` may return ``(html, text, headers)`` to personalise each
    message — that is how the unsubscribe link and its List-Unsubscribe headers
    end up specific to the recipient. Without it every address gets the same
    ``html``/``text``.

    A failing chunk is logged and the rest still go out: one bad address or a
    transient provider error must not cost the whole send.
    """
    settings = get_settings()
    if not recipients:
        return 0, 0

    def _for(address: str) -> tuple[str, str, dict]:
        if build is None:
            return html or "", text or "", {}
        return build(address)

    size = max(settings.email_batch_size, 1)
    chunks = [recipients[i:i + size] for i in range(0, len(recipients), size)]

    use_resend = settings.email_backend in ("resend", "fallback") and settings.resend_api_key
    sent = failed = 0

    for chunk in chunks:
        rendered = {address: _for(address) for address in chunk}

        if use_resend:
            messages = []
            for address, (body_html, body_text, extra_headers) in rendered.items():
                message = {
                    "from": settings.resend_from,
                    "to": [address],
                    "subject": subject,
                    "html": body_html,
                    "text": body_text,
                }
                if extra_headers:
                    message["headers"] = extra_headers
                messages.append(message)
            try:
                await _send_batch_via_resend(settings, messages)
                sent += len(chunk)
                continue
            except Exception as exc:  # noqa: BLE001 - one chunk must not stop the rest
                log.error(
                    "email.bulk.chunk_failed",
                    backend="resend", size=len(chunk), subject=subject, error=str(exc),
                )
                if settings.email_backend != "fallback":
                    failed += len(chunk)
                    continue

        # SMTP has no batch endpoint, and it is also where "fallback" lands
        # when the Resend chunk failed.
        for address, (body_html, body_text, extra_headers) in rendered.items():
            try:
                await send_email(
                    to=address, subject=subject, html=body_html, text=body_text,
                    headers=extra_headers or None,
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.error("email.bulk.failed", to=address, subject=subject, error=str(exc))
                failed += 1

    log.info("email.bulk.done", subject=subject, sent=sent, failed=failed)
    return sent, failed


async def _send_via_smtp(
    settings, to: str, subject: str, html: str, text: str, headers: dict | None = None
) -> None:
    def _send():
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from
        msg["To"] = to
        for key, value in (headers or {}).items():
            msg[key] = value
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.sendmail(settings.smtp_from, to, msg.as_string())

    await asyncio.to_thread(_send)


async def send_email(
    to: str, subject: str, html: str, text: str, headers: dict | None = None
) -> None:
    settings = get_settings()
    backend = settings.email_backend

    if backend == "fallback":
        await _send_with_fallback(settings, to, subject, html, text, headers)
        return

    if backend == "resend":
        if not settings.resend_api_key:
            log.warning("email.skipped", reason="RESEND_API_KEY not configured", to=to)
            return
        send_fn = _send_via_resend(settings, to, subject, html, text, headers)
    else:
        if not settings.smtp_user or not settings.smtp_password:
            log.warning("email.skipped", reason="SMTP credentials not configured", to=to)
            return
        send_fn = _send_via_smtp(settings, to, subject, html, text, headers)

    try:
        await send_fn
        log.info("email.sent", to=to, subject=subject, backend=backend)
    except httpx.HTTPStatusError as exc:
        log.error("email.failed", to=to, subject=subject, backend=backend,
                  status=exc.response.status_code, body=exc.response.text)
    except Exception as exc:
        log.error("email.failed", to=to, subject=subject, backend=backend, error=str(exc))


async def _send_with_fallback(
    settings, to: str, subject: str, html: str, text: str, headers: dict | None = None
) -> None:
    if not settings.resend_api_key:
        log.warning("email.fallback.resend_skipped", reason="RESEND_API_KEY not configured", to=to)
    else:
        try:
            await _send_via_resend(settings, to, subject, html, text, headers)
            log.info("email.sent", to=to, subject=subject, backend="resend")
            return
        except httpx.HTTPStatusError as exc:
            log.warning("email.fallback.resend_failed", to=to,
                        status=exc.response.status_code, body=exc.response.text)
        except Exception as exc:
            log.warning("email.fallback.resend_failed", to=to, error=str(exc))

    if not settings.smtp_user or not settings.smtp_password:
        log.error("email.failed", to=to, subject=subject,
                  reason="Resend failed and SMTP credentials not configured")
        return

    try:
        await _send_via_smtp(settings, to, subject, html, text, headers)
        log.info("email.sent", to=to, subject=subject, backend="smtp")
    except Exception as exc:
        log.error("email.failed", to=to, subject=subject, backend="smtp", error=str(exc))


# ── Shared footer snippets ─────────────────────────────────────────────────────

def _brand_footer(
    unsubscribe_email: str = "support@litcode.com.ng",
    unsubscribe_url: str | None = None,
) -> str:
    """Brand footer. Pass ``unsubscribe_url`` on marketing mail for a one-click
    link; without it the footer keeps the mailto, which is right for
    transactional mail nobody should be unsubscribing from."""
    year = datetime.now(timezone.utc).year
    unsubscribe_link = (
        f'<a href="{unsubscribe_url}"'
        f' style="color:#1FBF62;text-decoration:underline;font-size:11px;">Unsubscribe</a>'
        if unsubscribe_url
        else f'<a href="mailto:{unsubscribe_email}?subject=Unsubscribe"'
             f' style="color:#1FBF62;text-decoration:underline;font-size:11px;">Unsubscribe</a>'
    )
    return f"""
      <!-- BRAND FOOTER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px;border-radius:0 0 8px 8px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="width:40px;vertical-align:middle;">
              <img src="https://d2q7nhojr9v45l.cloudfront.net/logo/logo.png"
                   alt="Kida" width="36" height="36"
                   style="display:block;border-radius:4px;" />
            </td>
            <td style="padding-left:12px;vertical-align:middle;">
              <p style="margin:0;font-size:11px;font-weight:700;color:#fff;letter-spacing:0.1em;text-transform:uppercase;">KIDA</p>
              <p style="margin:2px 0 0 0;font-size:11px;color:#aaa;">Professional Music Production Samples</p>
            </td>
          </tr>
          <tr>
            <td colspan="2" style="padding-top:16px;border-top:1px solid #1f1f1f;margin-top:16px;">
              <p style="margin:12px 0 0 0;font-size:11px;color:#aaa;line-height:1.8;">
                Nigeria &middot; <a href="https://kida.litcode.com.ng" style="color:#1FBF62;text-decoration:none;">kida.litcode.com.ng</a><br>
                &copy; {year} Litcode. All rights reserved.<br>
                {unsubscribe_link}
              </p>
            </td>
          </tr>
        </table>
      </td></tr>"""


def _text_footer(unsubscribe_url: str | None = None) -> str:
    year = datetime.now(timezone.utc).year
    unsubscribe = (
        f"Unsubscribe: {unsubscribe_url}"
        if unsubscribe_url
        else "To unsubscribe, email support@litcode.com.ng with subject: Unsubscribe"
    )
    return (
        f"\n\n---\n"
        f"Kida | Professional Music Production Samples\n"
        f"Nigeria | kida.litcode.com.ng\n"
        f"© {year} Litcode. All rights reserved.\n"
        f"{unsubscribe}"
    )


# ── Templates ─────────────────────────────────────────────────────────────────

def verification_html(full_name: str, code: str, ttl_minutes: int) -> str:
    year = datetime.now(timezone.utc).year
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">VERIFY EMAIL</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#1FBF62;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">ALMOST THERE.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">Verify your</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#1FBF62;letter-spacing:-0.02em;">email.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{year} &nbsp;&middot;&nbsp; ACCOUNT SECURITY</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 8px 0;font-size:17px;font-weight:700;color:#0a0a0a;">Hi {full_name},</p>
        <p style="margin:0 0 24px 0;font-size:15px;line-height:1.6;color:#333;">
          Use the code below to verify your email and finish setting up your Kida account.
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 24px 0;">
          <tr><td align="center" style="background:#0a0a0a;padding:24px;border-radius:6px;">
            <p style="margin:0;font-size:40px;font-weight:800;letter-spacing:0.28em;color:#1FBF62;font-family:'Courier New',monospace;">{code}</p>
          </td></tr>
        </table>
        <p style="margin:0;font-size:13px;line-height:1.6;color:#777;">
          This code expires in {ttl_minutes} minutes. If you didn't create a Kida account, you can safely ignore this email.
        </p>
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def verification_text(full_name: str, code: str, ttl_minutes: int) -> str:
    return (
        f"Hi {full_name},\n\n"
        f"Use the code below to verify your email and finish setting up your Kida account.\n\n"
        f"Verification code: {code}\n\n"
        f"This code expires in {ttl_minutes} minutes.\n"
        f"If you didn't create a Kida account, you can safely ignore this email."
        + _text_footer()
    )


def registration_html(full_name: str) -> str:
    year = datetime.now(timezone.utc).year
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">WELCOME</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#1FBF62;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">YOU'RE IN.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">Make your</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#1FBF62;letter-spacing:-0.02em;">sound.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{year} &nbsp;&middot;&nbsp; PREMIUM LOOPS &nbsp;&middot;&nbsp; STEM PACKS</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 8px 0;font-size:17px;font-weight:700;color:#0a0a0a;">Hi {full_name},</p>
        <p style="margin:0 0 28px 0;font-size:15px;line-height:1.6;color:#333;">
          Your Kida account is ready. Browse premium loops and stem packs built for serious producers — no subscriptions, just the sounds you need.
        </p>
        <a href="https://kida.litcode.com.ng"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Browse Loops &rarr;
        </a>
      </td></tr>

      <!-- STATS -->
      <tr><td style="background:#f2ede4;padding:28px 32px;border-top:1px solid #ddd8ce;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="text-align:left;">
              <p style="margin:0;font-size:28px;font-weight:800;color:#0a0a0a;">500+</p>
              <p style="margin:4px 0 0 0;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#888;">Loops</p>
            </td>
            <td style="text-align:center;">
              <p style="margin:0;font-size:28px;font-weight:800;color:#0a0a0a;">10</p>
              <p style="margin:4px 0 0 0;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#888;">Genres</p>
            </td>
          </tr>
        </table>
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def registration_text(full_name: str) -> str:
    return (
        f"Hi {full_name},\n\n"
        f"Your Kida account is ready.\n\n"
        f"Browse premium loops and stem packs built for serious musicians — "
        f"you can start with no subscriptions, just the sounds you need.\n\n"
        f"Get started: https://kida.litcode.com.ng"
        + _text_footer()
    )


def new_user_admin_html(
    full_name: str,
    email: str,
    provider: str,
    user_id: str,
    signed_up_at: datetime,
) -> str:
    """Internal signup notification — plain and information-dense on purpose.

    Goes to the team inbox, not a customer, so it skips the marketing hero and
    the unsubscribe footer the customer-facing templates carry.
    """
    when = signed_up_at.strftime("%b %d, %Y at %H:%M UTC")
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">NEW SIGNUP</td>
          </tr>
        </table>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:32px;">
        <p style="margin:0 0 24px 0;font-size:17px;font-weight:700;color:#0a0a0a;">
          {full_name} just created an account.
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" style="font-size:14px;color:#333;">
          <tr>
            <td style="padding:8px 0;width:110px;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">Name</td>
            <td style="padding:8px 0;">{full_name}</td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">Email</td>
            <td style="padding:8px 0;"><a href="mailto:{email}" style="color:#1FBF62;text-decoration:none;">{email}</a></td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">Signed up via</td>
            <td style="padding:8px 0;">{provider}</td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">When</td>
            <td style="padding:8px 0;">{when}</td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">User ID</td>
            <td style="padding:8px 0;font-family:monospace;font-size:12px;color:#666;">{user_id}</td>
          </tr>
        </table>
      </td></tr>

      <!-- FOOTER -->
      <tr><td style="background:#0a0a0a;padding:20px 32px;border-radius:0 0 8px 8px;">
        <p style="margin:0;font-size:11px;color:#aaa;">
          Automated notification from the Kida API. Not a customer email.
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def new_user_admin_text(
    full_name: str,
    email: str,
    provider: str,
    user_id: str,
    signed_up_at: datetime,
) -> str:
    when = signed_up_at.strftime("%b %d, %Y at %H:%M UTC")
    return (
        f"{full_name} just created a Kida account.\n\n"
        f"Name:          {full_name}\n"
        f"Email:         {email}\n"
        f"Signed up via: {provider}\n"
        f"When:          {when}\n"
        f"User ID:       {user_id}\n\n"
        f"---\n"
        f"Automated notification from the Kida API. Not a customer email."
    )


def account_deleted_html(full_name: str) -> str:
    year = datetime.now(timezone.utc).year
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">GOODBYE</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#1FBF62;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">ACCOUNT DELETED.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">Until</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#1FBF62;letter-spacing:-0.02em;">next time.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{year} &nbsp;&middot;&nbsp; PREMIUM LOOPS &nbsp;&middot;&nbsp; STEM PACKS</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 8px 0;font-size:17px;font-weight:700;color:#0a0a0a;">Hi {full_name},</p>
        <p style="margin:0 0 28px 0;font-size:15px;line-height:1.6;color:#333;">
          Your Kida account has been permanently deleted. All your data has been removed from our systems.
          We're sorry to see you go — if you ever want to come back, you're always welcome.
        </p>
        <a href="https://kida.litcode.com.ng"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Come Back &rarr;
        </a>
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def account_deleted_text(full_name: str) -> str:
    return (
        f"Hi {full_name},\n\n"
        f"Your Kida account has been permanently deleted. "
        f"All your data has been removed from our systems.\n\n"
        f"We're sorry to see you go. If you ever want to come back: https://kida.litcode.com.ng"
        + _text_footer()
    )


def account_deleted_admin_html(
    full_name: str,
    email: str,
    provider: str,
    user_id: str,
    joined_at: datetime | None,
    deleted_at: datetime,
    actor: str = "user",
) -> str:
    """Internal account-deletion notification — the counterpart to new_user_admin_html.

    The account is already gone by the time this is built, so every value is
    passed in rather than looked up. ``actor`` distinguishes a self-deletion from
    an admin removal.
    """
    when = deleted_at.strftime("%b %d, %Y at %H:%M UTC")
    joined = joined_at.strftime("%b %d, %Y") if joined_at else "unknown"
    headline = (
        f"{full_name} was deleted by an admin."
        if actor == "admin"
        else f"{full_name} deleted their account."
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">ACCOUNT DELETED</td>
          </tr>
        </table>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:32px;">
        <p style="margin:0 0 24px 0;font-size:17px;font-weight:700;color:#0a0a0a;">
          {headline}
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" style="font-size:14px;color:#333;">
          <tr>
            <td style="padding:8px 0;width:110px;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">Name</td>
            <td style="padding:8px 0;">{full_name}</td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">Email</td>
            <td style="padding:8px 0;">{email}</td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">Signed up via</td>
            <td style="padding:8px 0;">{provider}</td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">Joined</td>
            <td style="padding:8px 0;">{joined}</td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">Deleted</td>
            <td style="padding:8px 0;">{when}</td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">Deleted by</td>
            <td style="padding:8px 0;">{actor}</td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#888;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;">User ID</td>
            <td style="padding:8px 0;font-family:monospace;font-size:12px;color:#666;">{user_id}</td>
          </tr>
        </table>
        <p style="margin:24px 0 0 0;font-size:13px;line-height:1.6;color:#666;">
          The account and all its data have been removed. This record is the only
          trace left — the user row no longer exists.
        </p>
      </td></tr>

      <!-- FOOTER -->
      <tr><td style="background:#0a0a0a;padding:20px 32px;border-radius:0 0 8px 8px;">
        <p style="margin:0;font-size:11px;color:#aaa;">
          Automated notification from the Kida API. Not a customer email.
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def account_deleted_admin_text(
    full_name: str,
    email: str,
    provider: str,
    user_id: str,
    joined_at: datetime | None,
    deleted_at: datetime,
    actor: str = "user",
) -> str:
    when = deleted_at.strftime("%b %d, %Y at %H:%M UTC")
    joined = joined_at.strftime("%b %d, %Y") if joined_at else "unknown"
    headline = (
        f"{full_name}'s Kida account was deleted by an admin."
        if actor == "admin"
        else f"{full_name} deleted their Kida account."
    )
    return (
        f"{headline}\n\n"
        f"Name:          {full_name}\n"
        f"Email:         {email}\n"
        f"Signed up via: {provider}\n"
        f"Joined:        {joined}\n"
        f"Deleted:       {when}\n"
        f"Deleted by:    {actor}\n"
        f"User ID:       {user_id}\n\n"
        f"The account and all its data have been removed. This record is the only\n"
        f"trace left — the user row no longer exists.\n\n"
        f"---\n"
        f"Automated notification from the Kida API. Not a customer email."
    )


def purchase_html(full_name: str, product_title: str, product_type: str, amount: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">PURCHASE</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#1FBF62;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">CONFIRMED.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">Your sound</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#1FBF62;letter-spacing:-0.02em;">is ready.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{datetime.now(timezone.utc).year} &nbsp;&middot;&nbsp; PREMIUM LOOPS &nbsp;&middot;&nbsp; STEM PACKS</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 20px 0;font-size:17px;font-weight:700;color:#0a0a0a;">Hi {full_name},</p>
        <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 28px 0;">
          <tr style="background:#e8e3d9;">
            <td style="padding:12px 16px;font-size:12px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:0.08em;width:40%;">Item</td>
            <td style="padding:12px 16px;font-size:14px;color:#0a0a0a;font-weight:600;">{product_title}</td>
          </tr>
          <tr>
            <td style="padding:12px 16px;font-size:12px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:0.08em;">Type</td>
            <td style="padding:12px 16px;font-size:14px;color:#0a0a0a;">{product_type}</td>
          </tr>
          <tr style="background:#e8e3d9;">
            <td style="padding:12px 16px;font-size:12px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:0.08em;">Amount Paid</td>
            <td style="padding:12px 16px;font-size:14px;color:#0a0a0a;font-weight:700;">&#8358;{amount}</td>
          </tr>
        </table>
        <p style="margin:0 0 28px 0;font-size:15px;line-height:1.6;color:#333;">
          Your purchase is available in your library immediately.
        </p>
        <a href="https://kida.litcode.com.ng"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Go to Library &rarr;
        </a>
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def purchase_text(full_name: str, product_title: str, product_type: str, amount: str) -> str:
    return (
        f"Hi {full_name},\n\n"
        f"Purchase confirmed!\n\n"
        f"Item: {product_title}\n"
        f"Type: {product_type}\n"
        f"Amount Paid: ₦{amount}\n\n"
        f"Your purchase is available in your library immediately.\n"
        f"Go to Library: https://kida.litcode.com.ng"
        + _text_footer()
    )


def newsletter_subscribe_html(email: str) -> str:
    year = datetime.now(timezone.utc).year
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">NEWSLETTER</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#1FBF62;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">YOU'RE SUBSCRIBED.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">Stay in</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#1FBF62;letter-spacing:-0.02em;">the loop.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{year} &nbsp;&middot;&nbsp; NEWS &nbsp;&middot;&nbsp; DROPS &nbsp;&middot;&nbsp; UPDATES</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 28px 0;font-size:15px;line-height:1.6;color:#333;">
          <strong>{email}</strong> is now subscribed to Kida news and marketing emails.
          You'll be the first to hear about new loops, stem packs, and exclusive drops.
        </p>
        <a href="https://kida.litcode.com.ng"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Browse Loops &rarr;
        </a>
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def newsletter_subscribe_text(email: str) -> str:
    return (
        f"You're subscribed!\n\n"
        f"{email} is now subscribed to Kida news and marketing emails.\n"
        f"You'll be the first to hear about new loops, stem packs, and exclusive drops.\n\n"
        f"Browse: https://kida.litcode.com.ng"
        + _text_footer()
    )


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


def newsletter_unsubscribe_html(email: str) -> str:
    year = datetime.now(timezone.utc).year
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">NEWSLETTER</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#1FBF62;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">UNSUBSCRIBED.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">We'll miss</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#1FBF62;letter-spacing:-0.02em;">you.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{year} &nbsp;&middot;&nbsp; NEWS &nbsp;&middot;&nbsp; DROPS &nbsp;&middot;&nbsp; UPDATES</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 28px 0;font-size:15px;line-height:1.6;color:#333;">
          <strong>{email}</strong> has been removed from Kida's mailing list.
          You won't receive any more marketing emails from us. Changed your mind?
        </p>
        <a href="https://kida.litcode.com.ng"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Resubscribe &rarr;
        </a>
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def newsletter_unsubscribe_text(email: str) -> str:
    return (
        f"You've been unsubscribed.\n\n"
        f"{email} has been removed from Kida's mailing list.\n"
        f"You won't receive any more marketing emails from us.\n\n"
        f"Changed your mind? Resubscribe at: https://kida.litcode.com.ng"
        + _text_footer()
    )


def content_digest_html(sections, total: int, unsubscribe_url: str | None = None) -> str:
    """The daily roundup: everything that went live, grouped by type.

    ``sections`` is a list of (content_type, label, items) where each item has
    ``title`` and an optional ``subtitle``. ``unsubscribe_url`` is signed for
    one recipient, so this is rendered per address rather than once per send.
    """
    blocks = []
    for _key, label, items in sections:
        rows = "".join(
            f"""
            <tr><td style="padding:6px 0;border-bottom:1px solid #eee;">
              <p style="margin:0;font-size:15px;font-weight:700;color:#0a0a0a;">{item.title}</p>
              {f'<p style="margin:2px 0 0 0;font-size:12px;color:#777;">{item.subtitle}</p>' if item.subtitle else ''}
            </td></tr>"""
            for item in items
        )
        blocks.append(f"""
          <tr><td style="padding:20px 0 6px 0;">
            <p style="margin:0;font-size:11px;font-weight:800;letter-spacing:0.14em;text-transform:uppercase;color:#1FBF62;">{label}</p>
          </td></tr>
          <tr><td><table width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>""")

    headline = "1 new drop on Kida" if total == 1 else f"{total} new drops on Kida"

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border-radius:8px;">
      <tr><td style="padding:32px 32px 8px 32px;">
        <p style="margin:0 0 6px 0;color:#1FBF62;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">FRESH DROPS</p>
        <h1 style="margin:0;font-size:24px;color:#0a0a0a;">{headline}</h1>
        <p style="margin:10px 0 0 0;font-size:14px;color:#555;line-height:1.6;">
          Here is everything that went live since the last roundup.
        </p>
      </td></tr>
      <tr><td style="padding:0 32px;"><table width="100%" cellpadding="0" cellspacing="0">{''.join(blocks)}</table></td></tr>
      <tr><td style="padding:0 32px 32px 32px;"></td></tr>
      {_brand_footer(unsubscribe_url=unsubscribe_url)}
    </table>
  </td></tr>
</table>
</body>
</html>"""


def content_digest_text(sections, total: int, unsubscribe_url: str | None = None) -> str:
    headline = "1 new drop on Kida" if total == 1 else f"{total} new drops on Kida"
    lines = [headline, "", "Everything that went live since the last roundup:", ""]
    for _key, label, items in sections:
        lines.append(f"{label.upper()}")
        for item in items:
            suffix = f" ({item.subtitle})" if item.subtitle else ""
            lines.append(f"  - {item.title}{suffix}")
        lines.append("")
    return "\n".join(lines).rstrip() + _text_footer(unsubscribe_url)


def account_suspended_html(full_name: str, reason: str) -> str:
    year = datetime.now(timezone.utc).year
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">ACCOUNT</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#e53e3e;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">SUSPENDED.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">Your account</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#e53e3e;letter-spacing:-0.02em;">is suspended.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{year} &nbsp;&middot;&nbsp; KIDA &nbsp;&middot;&nbsp; ACCOUNT NOTICE</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 8px 0;font-size:17px;font-weight:700;color:#0a0a0a;">Hi {full_name},</p>
        <p style="margin:0 0 16px 0;font-size:15px;line-height:1.6;color:#333;">
          Your Kida account has been suspended by our team. You will not be able to log in or access any content until the suspension is lifted.
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 28px 0;">
          <tr><td style="background:#fff0f0;border-left:3px solid #e53e3e;padding:14px 16px;border-radius:0 4px 4px 0;">
            <p style="margin:0 0 4px 0;font-size:11px;font-weight:700;color:#e53e3e;letter-spacing:0.1em;text-transform:uppercase;">REASON</p>
            <p style="margin:0;font-size:14px;color:#0a0a0a;">{reason}</p>
          </td></tr>
        </table>
        <a href="mailto:support@litcode.com.ng"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Contact Support &rarr;
        </a>
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def account_suspended_text(full_name: str, reason: str) -> str:
    return (
        f"Hi {full_name},\n\n"
        f"Your Kida account has been suspended by our team.\n\n"
        f"Reason: {reason}\n\n"
        f"You will not be able to log in or access any content until the suspension is lifted.\n"
        f"If you believe this is a mistake, please contact us at support@litcode.com.ng."
        + _text_footer()
    )


def account_unsuspended_html(full_name: str) -> str:
    year = datetime.now(timezone.utc).year
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">ACCOUNT</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#1FBF62;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">RESTORED.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">You're back</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#1FBF62;letter-spacing:-0.02em;">in.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{year} &nbsp;&middot;&nbsp; KIDA &nbsp;&middot;&nbsp; ACCOUNT NOTICE</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 8px 0;font-size:17px;font-weight:700;color:#0a0a0a;">Hi {full_name},</p>
        <p style="margin:0 0 28px 0;font-size:15px;line-height:1.6;color:#333;">
          Your Kida account has been reinstated. You can now log in and access all your content again.
          Welcome back — we're glad to have you.
        </p>
        <a href="https://kida.litcode.com.ng"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Go to Kida &rarr;
        </a>
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def account_unsuspended_text(full_name: str) -> str:
    return (
        f"Hi {full_name},\n\n"
        f"Your Kida account has been reinstated.\n\n"
        f"You can now log in and access all your content again.\n"
        f"Welcome back: https://kida.litcode.com.ng"
        + _text_footer()
    )


def _broadcast_paragraphs(body: str) -> str:
    """Render an admin-authored plain-text body as HTML paragraphs.

    The body is escaped — it is operator input, but it lands in a lot of
    inboxes, so a stray "<" should not be able to break the markup.
    """
    from html import escape

    blocks = [b.strip() for b in body.split("\n\n") if b.strip()]
    return "".join(
        '<p style="margin:0 0 16px 0;font-size:15px;line-height:1.6;color:#333;">'
        f'{escape(block).replace(chr(10), "<br>")}'
        "</p>"
        for block in blocks
    )


def broadcast_html(
    subject: str,
    body: str,
    heading: str | None = None,
    cta_label: str | None = None,
    cta_url: str | None = None,
) -> str:
    """An admin-authored announcement in the Kida shell."""
    from html import escape

    year = datetime.now(timezone.utc).year
    hero = escape(heading or subject)
    cta = ""
    if cta_label and cta_url:
        cta = (
            f'<a href="{escape(cta_url, quote=True)}" '
            'style="display:inline-block;margin-top:12px;padding:14px 28px;background:#0a0a0a;'
            'color:#fff;font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;'
            f'letter-spacing:0.02em;">{escape(cta_label)} &rarr;</a>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">NEWS</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;font-size:34px;font-weight:800;line-height:1.15;color:#fff;letter-spacing:-0.02em;">{hero}</p>
        <p style="margin:16px 0 0 0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{year} &nbsp;&middot;&nbsp; PREMIUM LOOPS &nbsp;&middot;&nbsp; STEM PACKS</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 32px 32px;">
        {_broadcast_paragraphs(body)}
        {cta}
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def broadcast_text(
    subject: str,
    body: str,
    heading: str | None = None,
    cta_label: str | None = None,
    cta_url: str | None = None,
) -> str:
    parts = [heading or subject, "", body]
    if cta_label and cta_url:
        parts += ["", f"{cta_label}: {cta_url}"]
    return "\n".join(parts) + _text_footer()


def _simple_notice_html(tag: str, hero_top: str, hero_accent: str, paragraphs: str) -> str:
    """Shared shell for the short account-status notices."""
    year = datetime.now(timezone.utc).year
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">{tag}</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;font-size:40px;font-weight:800;line-height:1.08;color:#fff;letter-spacing:-0.02em;">{hero_top}</p>
        <p style="margin:0;font-size:40px;font-weight:800;line-height:1.08;color:#1FBF62;letter-spacing:-0.02em;">{hero_accent}</p>
        <p style="margin:16px 0 0 0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{year} &nbsp;&middot;&nbsp; KIDA ACCOUNT</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 32px 32px;">
        {paragraphs}
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def deletion_request_html(confirm_url: str, ttl_minutes: int) -> str:
    """Confirmation link for a deletion asked for from the public web page.

    Addressed to nobody by name: the request arrives with an address and nothing
    else, and the person reading it may not be the account holder. Naming them
    would leak that the address is registered to whoever opened the mail.
    """
    window = f"{ttl_minutes} minutes" if ttl_minutes < 120 else f"{ttl_minutes // 60} hours"
    paragraphs = (
        '<p style="margin:0 0 20px 0;font-size:15px;line-height:1.6;color:#333;">'
        "Someone asked us to delete the Kida account registered to this address. "
        "If that was you, confirm below."
        "</p>"
        '<p style="margin:0 0 24px 0;font-size:15px;line-height:1.6;color:#333;">'
        f"This link works once and expires in <strong>{window}</strong>. Using it "
        "<strong>deletes the account straight away</strong> — there is no waiting "
        "period and it cannot be undone."
        "</p>"
        f'<a href="{confirm_url}" '
        'style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;'
        'font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;'
        'letter-spacing:0.02em;">Confirm deletion &rarr;</a>'
        '<p style="margin:24px 0 0 0;font-size:14px;line-height:1.6;color:#666;">'
        "Didn't ask for this? Ignore this email — nothing happens unless the link "
        "above is used, and we will not email you about it again."
        "</p>"
    )
    return _simple_notice_html("ACCOUNT DELETION", "Confirm your", "deletion.", paragraphs)


def deletion_request_text(confirm_url: str, ttl_minutes: int) -> str:
    window = f"{ttl_minutes} minutes" if ttl_minutes < 120 else f"{ttl_minutes // 60} hours"
    return (
        "Someone asked us to delete the Kida account registered to this address.\n"
        "If that was you, confirm with the link below.\n\n"
        f"{confirm_url}\n\n"
        f"This link works once and expires in {window}. Using it deletes the account\n"
        "straight away — there is no waiting period and it cannot be undone.\n\n"
        "Didn't ask for this? Ignore this email — nothing happens unless the link\n"
        "above is used, and we will not email you about it again."
        + _text_footer()
    )
