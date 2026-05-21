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


async def _send_via_resend(settings, to: str, subject: str, html: str) -> None:
    payload = {"from": settings.resend_from, "to": [to], "subject": subject, "html": html}
    headers = {"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(_RESEND_URL, json=payload, headers=headers)
        resp.raise_for_status()


async def _send_via_smtp(settings, to: str, subject: str, html: str) -> None:
    def _send():
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from
        msg["To"] = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.sendmail(settings.smtp_from, to, msg.as_string())

    await asyncio.to_thread(_send)


async def send_email(to: str, subject: str, html: str) -> None:
    settings = get_settings()
    backend = settings.email_backend

    if backend == "fallback":
        await _send_with_fallback(settings, to, subject, html)
        return

    if backend == "resend":
        if not settings.resend_api_key:
            log.warning("email.skipped", reason="RESEND_API_KEY not configured", to=to)
            return
        send_fn = _send_via_resend(settings, to, subject, html)
    else:
        if not settings.smtp_user or not settings.smtp_password:
            log.warning("email.skipped", reason="SMTP credentials not configured", to=to)
            return
        send_fn = _send_via_smtp(settings, to, subject, html)

    try:
        await send_fn
        log.info("email.sent", to=to, subject=subject, backend=backend)
    except httpx.HTTPStatusError as exc:
        log.error("email.failed", to=to, subject=subject, backend=backend,
                  status=exc.response.status_code, body=exc.response.text)
    except Exception as exc:
        log.error("email.failed", to=to, subject=subject, backend=backend, error=str(exc))


async def _send_with_fallback(settings, to: str, subject: str, html: str) -> None:
    if not settings.resend_api_key:
        log.warning("email.fallback.resend_skipped", reason="RESEND_API_KEY not configured", to=to)
    else:
        try:
            await _send_via_resend(settings, to, subject, html)
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
        await _send_via_smtp(settings, to, subject, html)
        log.info("email.sent", to=to, subject=subject, backend="smtp")
    except Exception as exc:
        log.error("email.failed", to=to, subject=subject, backend="smtp", error=str(exc))


# ── Templates ─────────────────────────────────────────────────────────────────

def registration_html(full_name: str) -> str:
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
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{datetime.now(timezone.utc).year} &nbsp;&middot;&nbsp; PREMIUM LOOPS &nbsp;&middot;&nbsp; STEM PACKS</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 8px 0;font-size:17px;font-weight:700;color:#0a0a0a;">Hi {full_name},</p>
        <p style="margin:0 0 28px 0;font-size:15px;line-height:1.6;color:#333;">
          Your kida account is ready. Browse premium loops and stem packs built for serious producers — no subscriptions, just the sounds you need.
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

      <!-- FOOTER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px;border-radius:0 0 8px 8px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="width:36px;vertical-align:middle;">
              <div style="width:32px;height:32px;background:#1FBF62;border-radius:4px;
                          text-align:center;line-height:32px;color:#fff;
                          font-size:12px;font-weight:800;letter-spacing:0.05em;">LM</div>
            </td>
            <td style="padding-left:12px;vertical-align:middle;">
              <p style="margin:0;font-size:11px;font-weight:700;color:#fff;letter-spacing:0.1em;text-transform:uppercase;">KIDA</p>
              <p style="margin:2px 0 0 0;font-size:11px;color:#fff;">Professional Music Production Samples</p>
            </td>
          </tr>
        </table>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def account_deleted_html(full_name: str) -> str:
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
        <p style="margin:0;color:#fff;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">{datetime.now(timezone.utc).year} &nbsp;&middot;&nbsp; PREMIUM LOOPS &nbsp;&middot;&nbsp; STEM PACKS</p>
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

      <!-- FOOTER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px;border-radius:0 0 8px 8px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="width:36px;vertical-align:middle;">
              <div style="width:32px;height:32px;background:#1FBF62;border-radius:4px;
                          text-align:center;line-height:32px;color:#fff;
                          font-size:12px;font-weight:800;letter-spacing:0.05em;">LM</div>
            </td>
            <td style="padding-left:12px;vertical-align:middle;">
              <p style="margin:0;font-size:11px;font-weight:700;color:#fff;letter-spacing:0.1em;text-transform:uppercase;">KIDA</p>
              <p style="margin:2px 0 0 0;font-size:11px;color:#fff;">Professional Music Production Samples</p>
            </td>
          </tr>
        </table>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def purchase_html(full_name: str, product_title: str, product_type: str, amount: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;max-width:600px;margin:auto;padding:24px;color:#1a1a1a;">
  <h1 style="color:#6c3bdb;">Purchase Confirmed ✓</h1>
  <p>Hi {full_name}, thanks for your purchase on Kida!</p>
  <table style="width:100%;border-collapse:collapse;margin:20px 0;border-radius:8px;overflow:hidden;">
    <tr style="background:#f5f0ff;">
      <td style="padding:12px 16px;font-weight:bold;width:40%;">Item</td>
      <td style="padding:12px 16px;">{product_title}</td>
    </tr>
    <tr>
      <td style="padding:12px 16px;font-weight:bold;">Type</td>
      <td style="padding:12px 16px;">{product_type}</td>
    </tr>
    <tr style="background:#f5f0ff;">
      <td style="padding:12px 16px;font-weight:bold;">Amount Paid</td>
      <td style="padding:12px 16px;">${amount}</td>
    </tr>
  </table>
  <p>Your purchase is available in your library immediately.</p>
  <a href="https://kida.litcode.com.ng"
     style="display:inline-block;margin-top:8px;padding:12px 28px;background:#6c3bdb;
            color:#fff;border-radius:6px;text-decoration:none;font-weight:bold;">
    Go to Library
  </a>
  <p style="margin-top:40px;color:#999;font-size:12px;">
    Kida &mdash; Professional Music Production Samples
  </p>
</body>
</html>"""
