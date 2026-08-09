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


async def _send_via_resend(settings, to: str, subject: str, html: str, text: str) -> None:
    payload = {
        "from": settings.resend_from,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    headers = {"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(_RESEND_URL, json=payload, headers=headers)
        resp.raise_for_status()


async def _send_via_smtp(settings, to: str, subject: str, html: str, text: str) -> None:
    def _send():
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from
        msg["To"] = to
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.sendmail(settings.smtp_from, to, msg.as_string())

    await asyncio.to_thread(_send)


async def send_email(to: str, subject: str, html: str, text: str) -> None:
    settings = get_settings()
    backend = settings.email_backend

    if backend == "fallback":
        await _send_with_fallback(settings, to, subject, html, text)
        return

    if backend == "resend":
        if not settings.resend_api_key:
            log.warning("email.skipped", reason="RESEND_API_KEY not configured", to=to)
            return
        send_fn = _send_via_resend(settings, to, subject, html, text)
    else:
        if not settings.smtp_user or not settings.smtp_password:
            log.warning("email.skipped", reason="SMTP credentials not configured", to=to)
            return
        send_fn = _send_via_smtp(settings, to, subject, html, text)

    try:
        await send_fn
        log.info("email.sent", to=to, subject=subject, backend=backend)
    except httpx.HTTPStatusError as exc:
        log.error("email.failed", to=to, subject=subject, backend=backend,
                  status=exc.response.status_code, body=exc.response.text)
    except Exception as exc:
        log.error("email.failed", to=to, subject=subject, backend=backend, error=str(exc))


async def _send_with_fallback(settings, to: str, subject: str, html: str, text: str) -> None:
    if not settings.resend_api_key:
        log.warning("email.fallback.resend_skipped", reason="RESEND_API_KEY not configured", to=to)
    else:
        try:
            await _send_via_resend(settings, to, subject, html, text)
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
        await _send_via_smtp(settings, to, subject, html, text)
        log.info("email.sent", to=to, subject=subject, backend="smtp")
    except Exception as exc:
        log.error("email.failed", to=to, subject=subject, backend="smtp", error=str(exc))


# ── Shared footer snippets ─────────────────────────────────────────────────────

def _brand_footer(unsubscribe_email: str = "support@litcode.com.ng") -> str:
    year = datetime.now(timezone.utc).year
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
                <a href="mailto:{unsubscribe_email}?subject=Unsubscribe"
                   style="color:#1FBF62;text-decoration:underline;font-size:11px;">Unsubscribe</a>
              </p>
            </td>
          </tr>
        </table>
      </td></tr>"""


def _text_footer() -> str:
    year = datetime.now(timezone.utc).year
    return (
        f"\n\n---\n"
        f"Kida | Professional Music Production Samples\n"
        f"Nigeria | kida.litcode.com.ng\n"
        f"© {year} Litcode. All rights reserved.\n"
        f"To unsubscribe, email support@litcode.com.ng with subject: Unsubscribe"
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


_CONTENT_TYPE_LABELS = {
    "loop": ("Loop", "LOOPS"),
    "drum_kit": ("Drum Kit", "DRUM KITS"),
    "drone_pad": ("Drone Pad", "DRONE PADS"),
}


def new_content_html(title: str, content_type: str) -> str:
    year = datetime.now(timezone.utc).year
    label, tag = _CONTENT_TYPE_LABELS.get(content_type, ("Content", "NEW"))
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
            <td align="right" style="color:#fff;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">NEW DROP</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#1FBF62;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">JUST DROPPED.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">New</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#1FBF62;letter-spacing:-0.02em;">{label}.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{year} &nbsp;&middot;&nbsp; PREMIUM {tag} &nbsp;&middot;&nbsp; KIDA</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 8px 0;font-size:17px;font-weight:700;color:#0a0a0a;">Fresh drop available now.</p>
        <p style="margin:0 0 28px 0;font-size:15px;line-height:1.6;color:#333;">
          <strong>{title}</strong>, a new {label.lower()} has just been added to Kida.
          Get it before everyone else does.
        </p>
        <a href="https://kida.litcode.com.ng"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Listen Now &rarr;
        </a>
      </td></tr>

      {_brand_footer()}

    </table>
  </td></tr>
</table>
</body>
</html>"""


def new_content_text(title: str, content_type: str) -> str:
    label, _ = _CONTENT_TYPE_LABELS.get(content_type, ("Content", "NEW"))
    return (
        f"Fresh drop on Kida!\n\n"
        f"{title}, a new {label.lower()} has just been added.\n"
        f"Get it before everyone else: https://kida.litcode.com.ng"
        + _text_footer()
    )


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
